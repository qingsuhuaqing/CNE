#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
服务器端代码：
1. 监听来自客户端的传输请求（格式：REQUEST:<filename>）。
2. 检查文件是否存在，根据文件大小选择传输协议：大于20K使用GBN，小于20K使用SR。
3. 以二进制模式读取文件，并将文件数据分成固定大小的包，
   每个包由头部（包含序号、总包数、分隔符"|DATA:"）和二进制数据构成。
4. 模拟丢包（LOSS_PROBABILITY）后发送数据包，通过ACK确认实现超时重传，
   保证丢失的数据包最终被正确补传，使得客户端能重组出完好的文件。
5. 传输结束后发送"END"结束标识，支持连续多次文件传输。
"""

import os
import socket
import time
import math
import random

# 配置参数
SERVER_IP = "0.0.0.0"      # 监听所有网卡
SERVER_PORT = 9000         # UDP端口
PACKET_SIZE = 1024         # 数据包中仅文件数据部分的大小（字节）
WINDOW_SIZE = 4            # 协议窗口大小
TIMEOUT = 0.5              # 协议内部超时设定（秒）
LOSS_PROBABILITY = 0.01    # 模拟数据包丢失率（1%）

def make_packet(seq, total, data):
    """
    构造数据包：
      - 头部格式为："SEQ:<seq>|TOTAL:<total>|DATA:"（使用UTF-8编码）
      - 后面紧跟二进制数据
    """
    header = f"SEQ:{seq}|TOTAL:{total}|DATA:".encode('utf-8')
    return header + data

def parse_request(msg):
    """
    解析客户端请求：
      格式要求为："REQUEST:<filename>"
      返回文件名字符串，如果格式错误返回 None
    """
    try:
        if msg.startswith("REQUEST:"):
            filename = msg.split(":", 1)[1].strip()
            return filename
    except Exception as e:
        print("请求解析错误：", e)
    return None

def send_init_response(sock, client_addr, protocol, file_size):
    """
    发送初始化响应给客户端，格式为："OK:<protocol>|SIZE:<file_size>"
    """
    resp = f"OK:{protocol}|SIZE:{file_size}"
    sock.sendto(resp.encode('utf-8'), client_addr)

def gbn_send(sock, client_addr, packets):
    """
    GBN协议传输数据：
      - 在窗口内发送所有未发送的数据包
      - 如果超时则重传窗口内所有包（体现GBN协议累计确认和重传后的机制）
    """
    base = 0
    next_seq = 0
    total = len(packets)
    retransmissions = 0
    start_time = time.time()
    send_times = [None] * total

    while base < total:
        # 在当前窗口内发送所有还未发送的数据包
        while next_seq < base + WINDOW_SIZE and next_seq < total:
            pkt = packets[next_seq]
            if random.random() > LOSS_PROBABILITY:
                sock.sendto(pkt, client_addr)
                print(f"GBN: 发送包 {next_seq}")
            else:
                print(f"GBN: 模拟丢包，未发送包 {next_seq}")
            send_times[next_seq] = time.time()
            next_seq += 1

        sock.settimeout(TIMEOUT)
        try:
            ack_data, _ = sock.recvfrom(1024)
            ack_msg = ack_data.decode('utf-8', errors='replace')
            if ack_msg.startswith("ACK:"):
                ack_num = int(ack_msg.split(":")[1])
                print(f"GBN: 收到ACK {ack_num}")
                # 更新基序号，如果有丢包，则连续ack前面的包还未到达客户端
                if ack_num >= base:
                    base = ack_num + 1
        except socket.timeout:
            # 超时则重传窗口内所有包
            print("GBN: 超时，重传窗口内所有包")
            retransmissions += (next_seq - base)
            next_seq = base
    total_time = time.time() - start_time
    return total_time, retransmissions, send_times

def sr_send(sock, client_addr, packets):
    """
    SR协议传输数据：
      - 每个数据包独立发送，收到ACK后将其标记为已确认
      - 超时后仅重传未确认的数据包（体现SR协议的选择重传机制）
    """
    total = len(packets)
    window = {}     # 当前窗口中已发送但未确认包（键为序号）
    acked = [False] * total
    retransmissions = 0
    start_time = time.time()
    send_times = [None] * total
    next_seq = 0

    while not all(acked):
        # 在窗口内发送未发送的包
        while len(window) < WINDOW_SIZE and next_seq < total:
            pkt = packets[next_seq]
            if random.random() > LOSS_PROBABILITY:
                sock.sendto(pkt, client_addr)
                print(f"SR: 发送包 {next_seq}")
            else:
                print(f"SR: 模拟丢包，未发送包 {next_seq}")
            send_times[next_seq] = time.time()
            window[next_seq] = pkt
            next_seq += 1

        sock.settimeout(TIMEOUT)
        try:
            ack_data, _ = sock.recvfrom(1024)
            ack_msg = ack_data.decode('utf-8', errors='replace')
            if ack_msg.startswith("ACK:"):
                ack_num = int(ack_msg.split(":")[1])
                if 0 <= ack_num < total and not acked[ack_num]:
                    acked[ack_num] = True
                    if ack_num in window:
                        del window[ack_num]
                    print(f"SR: 收到ACK {ack_num}")
        except socket.timeout:
            # 超时后只重传当前窗口内未确认的数据包
            for seq in list(window.keys()):
                if not acked[seq]:
                    pkt = window[seq]
                    if random.random() > LOSS_PROBABILITY:
                        sock.sendto(pkt, client_addr)
                        print(f"SR: 超时重传包 {seq}")
                    else:
                        print(f"SR: 模拟丢包，未重传包 {seq}")
                    retransmissions += 1
                    send_times[seq] = time.time()
    total_time = time.time() - start_time
    return total_time, retransmissions, send_times

def main():
    # 创建UDP套接字，设置为阻塞模式以持续等待新请求
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((SERVER_IP, SERVER_PORT))
    print(f"服务器已启动，监听 {SERVER_IP}:{SERVER_PORT}")

    while True:
        try:
            # 等待客户端请求（阻塞调用，不设超时）
            data, client_addr = sock.recvfrom(4096)
            request = data.decode('utf-8', errors='replace')
            print(f"\n收到来自 {client_addr} 的请求: {request}")
            filename = parse_request(request)
            if not filename:
                print("请求格式错误")
                continue

            # 检查文件是否存在
            if not os.path.isfile(filename):
                err_msg = "ERROR:文件不存在"
                sock.sendto(err_msg.encode('utf-8'), client_addr)
                print(f"文件 {filename} 不存在，已通知客户端。")
                continue

            file_size = os.path.getsize(filename)
            # 根据要求：大于20K使用GBN，小于520K使用SR
            protocol = "SR" if file_size < 20*1024 else "GBN"
            send_init_response(sock, client_addr, protocol, file_size)
            print(f"开始传输文件 {filename}，大小 {file_size} 字节，使用协议：{protocol}")

            # 以二进制模式读取文件，并分包
            with open(filename, "rb") as f:
                file_data = f.read()
            print(f"读取文件成功，大小：{len(file_data)} 字节")
            total_packets = math.ceil(len(file_data) / PACKET_SIZE)
            packets = []
            for i in range(total_packets):
                chunk = file_data[i * PACKET_SIZE : (i + 1) * PACKET_SIZE]
                pkt = make_packet(i, total_packets, chunk)
                packets.append(pkt)
                print(f"生成包 {i}, 数据长度：{len(chunk)} 字节")

            # 根据选定的协议传输数据包，并记录重传情况
            if protocol == "GBN":
                total_time, retransmissions, send_times = gbn_send(sock, client_addr, packets)
            else:
                total_time, retransmissions, send_times = sr_send(sock, client_addr, packets)

            # 传输完成后发送结束标识
            sock.sendto("END".encode('utf-8'), client_addr)
            print("传输完成，结束信号已发送。")
            print(f"传输总时间：{total_time:.2f}秒，重传次数：{retransmissions}")
            # 在客户端侧（实际使用时）应按包头中的 SEQ 对数据进行重排序，
            # 移除包头后将所有数据拼接得到完整的文件，这里保证了即使有丢包，
            # 重传机制也最终恢复了所有丢失的包，确保文件完好。

        except socket.timeout:
            # 如果在等待请求时发生超时（不应出现，因为处于阻塞模式），则忽略
            continue
        except Exception as e:
            # 捕获单次传输过程中的异常，输出后继续等待下一次请求
            print("发生异常：", e)
            continue

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
服务器端支持双向文件传输功能：
1. 客户端请求格式：
   - DOWNLOAD:<filename>   从服务器下载文件；
   - UPLOAD_GBN:<filename>   上传文件，较大文件（>20K，客户端选择GBN上传）；
   - UPLOAD_SR:<filename>    上传文件，较小文件（<=20K，客户端选择SR上传）。
2. 下载模式：服务器检查文件存在性，根据文件大小选择协议（GBN或SR）进行传输；
3. 上传模式：服务器根据请求（UPLOAD_GBN 或 UPLOAD_SR）调用对应接收函数，
   实现接收数据包、ACK确认、数据重组，最后保存文件（文件名前加"uploaded_"）。
4. UDP传输中所有数据包均在头部以UTF-8编码，后附原始二进制数据，避免二进制文件解码错误。
5. 传输结束后，服务器持续等待下一个请求。
"""

import os
import socket
import time
import math
import random
import re

# 配置参数
SERVER_IP = "0.0.0.0"  # 监听所有网卡
SERVER_PORT = 9000     # UDP端口
PACKET_SIZE = 1024     # 数据包中仅文件数据部分的大小（字节）
WINDOW_SIZE = 4        # 协议窗口大小（仅用于发送时）
TIMEOUT = 0.5          # 协议内部超时设定（秒）
LOSS_PROBABILITY = 0.01 # 模拟数据包丢失率（1%）

######################
# 通用工具函数
######################
def make_packet(seq, total, data):
    """
    构造数据包：
      头部格式："SEQ:<seq>|TOTAL:<total>|DATA:"（UTF-8编码）
      后续追加原始二进制数据
    """
    header = f"SEQ:{seq}|TOTAL:{total}|DATA:".encode('utf-8')
    return header + data

def parse_packet(data):
    """
    解析数据包：
      - 定位 header 与二进制数据之间的分隔符 "|DATA:"
      - 对 header 部分进行 decode() 得到序号和总包数，其余部分保持二进制
    """
    header_end = data.find(b"|DATA:")
    if header_end == -1:
        print("解析失败：未找到 '|DATA:' 分隔符")
        return None, None, None
    header = data[:header_end].decode('utf-8', errors='replace')
    content = data[header_end + 6:]
    seq_match = re.search(r"SEQ:(\d+)", header)
    total_match = re.search(r"TOTAL:(\d+)", header)
    if seq_match and total_match:
        seq = int(seq_match.group(1))
        total = int(total_match.group(1))
        return seq, total, content
    else:
        return None, None, None

######################
# 下载方向（服务器发送）函数
######################
def send_init_response(sock, client_addr, protocol, file_size):
    """
    发送初始化响应给客户端下载，格式："OK:<protocol>|SIZE:<file_size>"
    """
    resp = f"OK:{protocol}|SIZE:{file_size}"
    sock.sendto(resp.encode('utf-8'), client_addr)

def gbn_send(sock, client_addr, packets):
    """
    使用改进后的 Go-Back-N 协议发送数据包（用于下载）：
    - 采用窗口发送，每个包的确认状态存入 ack_received 数组
    - 超时后仅重传窗口内未确认的包，不会直接舍弃丢失包
    """
    base = 0
    next_seq = 0
    total = len(packets)
    retransmissions = 0
    ack_received = [False] * total
    start_time = time.time()

    while base < total:
        # 在窗口内发送未确认的包
        while next_seq < base + WINDOW_SIZE and next_seq < total:
            if not ack_received[next_seq]:
                pkt = packets[next_seq]
                # 模拟丢包发送
                if random.random() > LOSS_PROBABILITY:
                    sock.sendto(pkt, client_addr)
                    print(f"GBN（下载）：发送包 {next_seq}")
                else:
                    print(f"GBN（下载）：模拟丢包，未发送包 {next_seq}")
            next_seq += 1

        sock.settimeout(TIMEOUT)
        try:
            ack_data, _ = sock.recvfrom(1024)
            ack_msg = ack_data.decode('utf-8', errors='replace')
            if ack_msg.startswith("ACK:"):
                ack_num = int(ack_msg.split(":")[1])
                print(f"GBN（下载）：收到ACK {ack_num}")
                if 0 <= ack_num < total:
                    ack_received[ack_num] = True
                    # 仅当连续包已确认时，滑动窗口
                    while base < total and ack_received[base]:
                        base += 1
        except socket.timeout:
            print("GBN（下载）：超时，重传窗口内未确认的包")
            for i in range(base, min(base + WINDOW_SIZE, total)):
                if not ack_received[i]:
                    pkt = packets[i]
                    if random.random() > LOSS_PROBABILITY:
                        sock.sendto(pkt, client_addr)
                        print(f"GBN（下载）：重传包 {i}")
                    else:
                        print(f"GBN（下载）：模拟丢包，重传包 {i} 未发送")
                    retransmissions += 1
            next_seq = base  # 重新从窗口起点开始发送

    total_time = time.time() - start_time
    return total_time, retransmissions

def sr_send(sock, client_addr, packets):
    """
    使用选择性重传协议（SR）发送数据包（用于下载）：
    - 每个数据包独立发送，收到ACK后确认；超时后仅重传未确认包
    """
    total = len(packets)
    window = {}  # 存放当前窗口中未确认的包，键为序号
    acked = [False] * total
    retransmissions = 0
    next_seq = 0
    start_time = time.time()

    while not all(acked):
        while len(window) < WINDOW_SIZE and next_seq < total:
            pkt = packets[next_seq]
            if random.random() > LOSS_PROBABILITY:
                sock.sendto(pkt, client_addr)
                print(f"SR（下载）：发送包 {next_seq}")
            else:
                print(f"SR（下载）：模拟丢包，未发送包 {next_seq}")
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
                    print(f"SR（下载）：收到ACK {ack_num}")
        except socket.timeout:
            for seq in list(window.keys()):
                if not acked[seq]:
                    pkt = window[seq]
                    if random.random() > LOSS_PROBABILITY:
                        sock.sendto(pkt, client_addr)
                        print(f"SR（下载）：超时重传包 {seq}")
                    else:
                        print(f"SR（下载）：模拟丢包，未重传包 {seq}")
                    retransmissions += 1

    total_time = time.time() - start_time
    return total_time, retransmissions

def handle_download(sock, client_addr, filename):
    """
    处理下载请求：服务器发送文件给客户端
    """
    if not os.path.isfile(filename):
        err_msg = "ERROR:文件不存在"
        sock.sendto(err_msg.encode('utf-8'), client_addr)
        print(f"文件 {filename} 不存在，通知客户端下载。")
        return

    file_size = os.path.getsize(filename)
    # 这里可根据实际需求调整阈值，示例中大文件使用GBN，小文件使用SR
    protocol = "SR" if file_size < 20*1024 else "GBN"
    send_init_response(sock, client_addr, protocol, file_size)
    print(f"开始传输文件 {filename}，大小 {file_size} 字节，使用协议：{protocol}")

    with open(filename, "rb") as f:
        file_data = f.read()
    total_packets = math.ceil(len(file_data) / PACKET_SIZE)
    packets = []
    for i in range(total_packets):
        chunk = file_data[i * PACKET_SIZE:(i + 1) * PACKET_SIZE]
        pkt = make_packet(i, total_packets, chunk)
        packets.append(pkt)
        print(f"生成下载包 {i}, 数据长度：{len(chunk)} 字节")

    if protocol == "GBN":
        total_time, retransmissions = gbn_send(sock, client_addr, packets)
    else:
        total_time, retransmissions = sr_send(sock, client_addr, packets)

    sock.sendto("END".encode('utf-8'), client_addr)
    print(f"文件 {filename} 传输完成，结束信号已发送。\n下载耗时 {total_time:.3f} 秒, 重传次数 {retransmissions}")

######################
# 上传方向（服务器接收）函数
######################
def handle_upload_sr(sock, client_addr, filename):
    """
    处理上传请求（SR模式）：服务器接收文件（允许乱序接收）
    """
    sock.sendto("READY".encode('utf-8'), client_addr)
    print(f"通知客户端上传文件 {filename}（SR模式）")
    received_packets = {}
    expected_total = None
    first_packet_time = None
    last_packet_time = None

    while True:
        try:
            packet_data, addr = sock.recvfrom(4096)
            try:
                if packet_data.decode('utf-8', errors='replace') == "END":
                    print("SR上传：收到结束信号")
                    break
            except Exception:
                pass

            seq, total, content = parse_packet(packet_data)
            if seq is None:
                continue
            if expected_total is None:
                expected_total = total
            if first_packet_time is None:
                first_packet_time = time.time()
            last_packet_time = time.time()
            if seq not in received_packets:
                received_packets[seq] = content
            ack_msg = f"ACK:{seq}"
            sock.sendto(ack_msg.encode('utf-8'), client_addr)
            print(f"SR上传：收到包 {seq} (共 {total} 包)")
        except socket.timeout:
            continue
        except Exception as e:
            print("SR上传过程中异常：", e)
            break

    file_data = b""
    for seq in sorted(received_packets.keys()):
        file_data += received_packets[seq]
    local_filename = "uploaded_" + filename
    with open(local_filename, "wb") as f:
        f.write(file_data)
    duration = (last_packet_time - first_packet_time) if first_packet_time and last_packet_time else 0.001
    print(f"SR上传成功，已保存为 {local_filename}\n上传耗时 {duration:.3f} 秒")

def handle_upload_gbn(sock, client_addr, filename):
    """
    处理上传请求（GBN模式）：服务器接收文件（要求严格按序）
    """
    sock.sendto("READY".encode('utf-8'), client_addr)
    print(f"通知客户端上传文件 {filename}（GBN模式）")
    expected_seq = 0
    received_data = []
    first_packet_time = None
    last_packet_time = None

    while True:
        try:
            packet_data, addr = sock.recvfrom(4096)
            try:
                if packet_data.decode('utf-8', errors='replace') == "END":
                    print("GBN上传：收到结束信号")
                    break
            except Exception:
                pass

            seq, total, content = parse_packet(packet_data)
            if seq is None:
                continue
            if first_packet_time is None:
                first_packet_time = time.time()
            last_packet_time = time.time()
            if seq == expected_seq:
                received_data.append(content)
                expected_seq += 1
                print(f"GBN上传：正确接收到包 {seq}")
            else:
                print(f"GBN上传：收到乱序包 {seq}，期望 {expected_seq}")
            ack_msg = f"ACK:{expected_seq - 1}"
            sock.sendto(ack_msg.encode('utf-8'), client_addr)
        except socket.timeout:
            continue
        except Exception as e:
            print("GBN上传过程中异常：", e)
            break

    file_data = b"".join(received_data)
    local_filename = "uploaded_" + filename
    with open(local_filename, "wb") as f:
        f.write(file_data)
    duration = (last_packet_time - first_packet_time) if first_packet_time and last_packet_time else 0.001
    print(f"GBN上传成功，已保存为 {local_filename}\n上传耗时 {duration:.3f} 秒")

######################
# 服务器主程序
######################
def main():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((SERVER_IP, SERVER_PORT))
    print(f"服务器已启动，监听 {SERVER_IP}:{SERVER_PORT}")

    while True:
        try:
            data, client_addr = sock.recvfrom(4096)
            request = data.decode('utf-8', errors='replace')
            print(f"\n收到来自 {client_addr} 的请求: {request}")

            if request.startswith("DOWNLOAD:"):
                filename = request.split(":", 1)[1].strip()
                handle_download(sock, client_addr, filename)
            elif request.startswith("UPLOAD_GBN:"):
                filename = request.split(":", 1)[1].strip()
                handle_upload_gbn(sock, client_addr, filename)
            elif request.startswith("UPLOAD_SR:"):
                filename = request.split(":", 1)[1].strip()
                handle_upload_sr(sock, client_addr, filename)
            else:
                print("未知请求格式")
                sock.sendto("ERROR:未知请求格式".encode('utf-8'), client_addr)
        except socket.timeout:
            continue
        except Exception as e:
            print("发生异常：", e)
            continue

if __name__ == "__main__":
    main()

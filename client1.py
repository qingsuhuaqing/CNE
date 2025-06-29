#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#可以进行相应的上传下载,但是关于传输性能的描述稍显简略.
"""
客户端支持双向文件传输：
1. 用户选择操作：
   - DOWNLOAD：从服务器下载文件
   - UPLOAD  ：将本地文件上传到服务器
2. 对于上传操作：客户端先获取本地待上传文件大小，
   若大于20K则选择GBN上传（即服务器返回协议为GBN），否则选择SR上传，
   并在请求中使用不同前缀（UPLOAD_GBN 或 UPLOAD_SR）。
3. 下载与上传均采用数据包中只对头部进行解码、保留二进制数据的方式，
   以确保文档、图片等二进制文件能正确传输，即使出现丢包也会触发重传。
"""

import os
import socket
import time
import re
import math
import random

# 配置参数
SERVER_IP = "8.146.198.147"  # 修改为实际服务器IP
SERVER_PORT = 9000
PACKET_SIZE = 1024  # 与服务器保持一致
TIMEOUT = 2  # 超时设置（秒）
LOSS_PROBABILITY = 0.01  # 上传丢包模拟（下载时不用模拟丢包，下载时主要依赖重传机制）


######################
# 公共函数
######################
def parse_init_response(msg):
    """
    解析服务器返回的初始化响应，例如 "OK:SR|SIZE:123456"
    返回：protocol, file_size
    """
    try:
        parts = msg.split("|")
        proto = parts[0].split(":")[1]
        size = int(parts[1].split(":")[1])
        return proto, size
    except Exception as e:
        print("解析初始化响应失败：", e)
        return None, None


def parse_packet(data):
    """
    解析数据包：
      - 定位头部与二进制数据之间的分隔符 "|DATA:"
      - 仅对头部进行 decode() ，数据部分保持二进制
    返回：序号, 总包数, 数据内容
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


def make_packet(seq, total, data):
    """
    构造数据包：头部格式"SEQ:<seq>|TOTAL:<total>|DATA:"（UTF-8编码）+ 二进制数据
    """
    header = f"SEQ:{seq}|TOTAL:{total}|DATA:".encode('utf-8')
    return header + data


######################
# 下载函数（客户端作为接收端）
######################
def download_file(sock, server_addr, filename):
    req_msg = f"DOWNLOAD:{filename}"
    sock.sendto(req_msg.encode('utf-8'), server_addr)

    try:
        data, _ = sock.recvfrom(1024)
        init_msg = data.decode('utf-8', errors='replace')
        if init_msg.startswith("ERROR"):
            print("服务器返回错误:", init_msg)
            return
        protocol, file_size = parse_init_response(init_msg)
        print(f"服务器选择协议：{protocol}，文件大小：{file_size} 字节")
    except socket.timeout:
        print("等待服务器响应超时")
        return

    received_packets = {}  # 保存数据包内容，键为包序号
    expected_total = None
    first_packet_time = None
    last_packet_time = None

    # 针对GBN模式使用累计ACK；SR模式直接对每个包ACK
    if protocol == "GBN":
        expected_seq = 0  # 期望的下一个包号
        print("使用GBN累计ACK模式接收下载文件")
        while True:
            try:
                packet_data, _ = sock.recvfrom(4096)
                # 判断是否为结束信号
                try:
                    if packet_data.decode('utf-8', errors='replace') == "END":
                        print("收到结束信号")
                        break
                except Exception:
                    pass

                seq, total, content = parse_packet(packet_data)
                if seq is None:
                    continue
                if expected_total is None:
                    expected_total = total
                    print(f"本次下载总包数：{expected_total}")
                if first_packet_time is None:
                    first_packet_time = time.time()
                last_packet_time = time.time()

                # 仅接受正好按序到达的包
                if seq == expected_seq:
                    received_packets[seq] = content
                    expected_seq += 1
                    print(f"下载(GBN): 按序接收包 {seq}")
                else:
                    print(f"下载(GBN): 收到乱序包 {seq}，期望 {expected_seq}，忽略此包")
                # 始终回复上一个正确接收包的累计ACK
                ack_msg = f"ACK:{expected_seq - 1}"
                sock.sendto(ack_msg.encode('utf-8'), server_addr)
            except socket.timeout:
                print("下载(GBN)：等待数据包超时，继续监听……")
                continue
            except Exception as e:
                print("下载(GBN)：错误：", e)
                break
    else:
        # SR模式：对收到的每个包独立ACK
        print("使用SR模式接收下载文件")
        while True:
            try:
                packet_data, _ = sock.recvfrom(4096)
                try:
                    if packet_data.decode('utf-8', errors='replace') == "END":
                        print("收到结束信号")
                        break
                except Exception:
                    pass

                seq, total, content = parse_packet(packet_data)
                if seq is None:
                    continue
                if expected_total is None:
                    expected_total = total
                    print(f"本次下载总包数：{expected_total}")
                if first_packet_time is None:
                    first_packet_time = time.time()
                last_packet_time = time.time()

                if seq not in received_packets:
                    received_packets[seq] = content
                    print(f"下载(SR): 接收包 {seq}")
                ack_msg = f"ACK:{seq}"
                sock.sendto(ack_msg.encode('utf-8'), server_addr)
            except socket.timeout:
                print("下载(SR)：等待数据包超时，继续监听……")
                continue
            except Exception as e:
                print("下载(SR)：错误：", e)
                break

    # 文件重组
    file_data = b""
    for seq in sorted(received_packets.keys()):
        file_data += received_packets[seq]
    local_filename = "received_" + filename
    with open(local_filename, "wb") as f:
        f.write(file_data)
    print(f"下载文件已保存为 {local_filename}")

    # 可选：统计传输时间等
    if first_packet_time and last_packet_time:
        transmission_time = last_packet_time - first_packet_time
    else:
        transmission_time = 0.001
    print(f"传输耗时 {transmission_time:.3f} 秒")


######################
# 上传发送函数（客户端作为发送端）——GBN模式
######################
def upload_gbn_send(sock, server_addr, packets):
    base = 0
    next_seq = 0
    total = len(packets)
    while base < total:
        while next_seq < base + 4 and next_seq < total:
            pkt = packets[next_seq]
            if random.random() > LOSS_PROBABILITY:
                sock.sendto(pkt, server_addr)
                print(f"GBN上传：发送包 {next_seq}")
            else:
                print(f"GBN上传：模拟丢包，未发送包 {next_seq}")
            next_seq += 1
        sock.settimeout(TIMEOUT)
        try:
            ack_data, _ = sock.recvfrom(1024)
            ack_msg = ack_data.decode('utf-8', errors='replace')
            if ack_msg.startswith("ACK:"):
                ack_num = int(ack_msg.split(":")[1])
                print(f"GBN上传：收到ACK {ack_num}")
                if ack_num >= base:
                    base = ack_num + 1
        except socket.timeout:
            print("GBN上传：超时，重传当前窗口")
            next_seq = base
    sock.sendto("END".encode('utf-8'), server_addr)
    print("GBN上传完成，结束信号已发送。")


######################
# 上传发送函数（客户端作为发送端）——SR模式
######################
def upload_sr_send(sock, server_addr, packets):
    total = len(packets)
    window = {}
    acked = [False] * total
    next_seq = 0
    while not all(acked):
        while len(window) < 4 and next_seq < total:
            pkt = packets[next_seq]
            if random.random() > LOSS_PROBABILITY:
                sock.sendto(pkt, server_addr)
                print(f"SR上传：发送包 {next_seq}")
            else:
                print(f"SR上传：模拟丢包，未发送包 {next_seq}")
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
                    print(f"SR上传：收到ACK {ack_num}")
        except socket.timeout:
            for seq in list(window.keys()):
                sock.sendto(window[seq], server_addr)
                print(f"SR上传：超时重传包 {seq}")
        # 循环直至所有包均被ACK
    sock.sendto("END".encode('utf-8'), server_addr)
    print("SR上传完成，结束信号已发送。")


######################
# 上传函数（客户端发送文件）
######################
def upload_file(sock, server_addr, filename):
    if not os.path.isfile(filename):
        print(f"本地文件 {filename} 不存在")
        return

    file_size = os.path.getsize(filename)
    # 依据上传文件大小选择协议（20KB为阈值）
    if file_size > 20 * 1024:
        protocol_tag = "UPLOAD_GBN"
        chosen_protocol = "GBN"
    else:
        protocol_tag = "UPLOAD_SR"
        chosen_protocol = "SR"
    req_msg = f"{protocol_tag}:{filename}"
    sock.sendto(req_msg.encode('utf-8'), server_addr)

    # 等待服务器就绪信号
    try:
        ready_msg, _ = sock.recvfrom(1024)
        if ready_msg.decode('utf-8', errors='replace') != "READY":
            print("服务器未发出就绪信号")
            return
        print("服务器就绪，开始上传文件")
    except socket.timeout:
        print("等待服务器就绪超时")
        return

    with open(filename, "rb") as f:
        file_data = f.read()
    total_packets = math.ceil(len(file_data) / PACKET_SIZE)
    packets = []
    for i in range(total_packets):
        chunk = file_data[i * PACKET_SIZE:(i + 1) * PACKET_SIZE]
        pkt = make_packet(i, total_packets, chunk)
        packets.append(pkt)
        print(f"上传：生成包 {i}, 数据长度：{len(chunk)} 字节")

    if chosen_protocol == "GBN":
        upload_gbn_send(sock, server_addr, packets)
    else:
        upload_sr_send(sock, server_addr, packets)


######################
# 主程序
######################
def main():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(TIMEOUT)
    server_ip = input(f"请输入服务器IP（默认 {SERVER_IP}）：") or SERVER_IP
    sp = input(f"请输入服务器端口（默认 {SERVER_PORT}）：")
    server_port = int(sp) if sp else SERVER_PORT
    server_addr = (server_ip, server_port)

    op = input("请选择操作类型 (DOWNLOAD/UPLOAD): ").strip().upper()
    filename = input("请输入文件名：").strip()

    if op == "DOWNLOAD":
        download_file(sock, server_addr, filename)
    elif op == "UPLOAD":
        upload_file(sock, server_addr, filename)
    else:
        print("未知操作类型")

    sock.close()


if __name__ == "__main__":
    main()

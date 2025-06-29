#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#只支持下载,但是关于传输性能的描述更为详细
"""
客户端代码：
1. 向服务器发送传输请求（格式：REQUEST:<filename>），接收服务器返回的协议选择与文件大小信息。
2. 根据服务器返回信息依次接收数据包，并发送累计ACK确认。
3. 每个数据包仅对头部进行解码，数据部分作为原始二进制数据保存。
4. 接收到结束标识"END"后，将各数据包按序重组保存为文件，同时统计传输指标。
   采用累计ACK机制：客户端只接受按序到达的包，并始终回复上一个连续包的ACK，迫使服务器重传中间丢失的包。
"""

import socket
import time
import re

# 配置参数
SERVER_IP = "8.146.198.147"    # 根据实际服务器IP修改
SERVER_PORT = 9000
PACKET_SIZE = 1024             # 保持与服务器端一致
TIMEOUT = 2                    # 客户端接收超时（秒）

def parse_init_response(msg):
    """
    解析服务器返回的初始化响应，例如 "OK:SR|SIZE:123456"
    返回：protocol 和 file_size
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
      - 只对头部调用 decode() 提取序号与总包数，数据部分原样返回
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
        print("解析错误：头部中缺少序号或总包数")
        return None, None, None

def main():
    # 创建UDP套接字，并设置接收超时
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(TIMEOUT)

    # 输入服务器信息与待传输文件名
    server_ip = input(f"请输入服务器IP（默认 {SERVER_IP}）：") or SERVER_IP
    sp = input(f"请输入服务器端口（默认 {SERVER_PORT}）：")
    server_port = int(sp) if sp else SERVER_PORT
    filename = input("请输入传输的文件名：")
    server_addr = (server_ip, server_port)

    # 发送文件传输请求
    req_msg = f"REQUEST:{filename}"
    sock.sendto(req_msg.encode('utf-8'), server_addr)

    # 等待接收服务器初始化响应
    try:
        data, _ = sock.recvfrom(1024)
        init_msg = data.decode('utf-8', errors='replace')
        if init_msg.startswith("ERROR"):
            print("服务器返回错误:", init_msg)
            return
        protocol, file_size = parse_init_response(init_msg)
        if protocol is None:
            return
        print(f"服务器选择协议：{protocol}，文件大小：{file_size} 字节")
    except socket.timeout:
        print("等待服务器响应超时")
        return

    # 初始化累计ACK变量
    expected_seq = 0

    # 准备存储数据包的字典（仅保存按序的数据）
    received_packets = {}
    expected_total = None
    first_packet_time = None
    last_packet_time = None
    rtt_list = []      # 模拟RTT，可根据实际时间戳计算
    total_acks = 0

    # 记录最后一次收到数据包的时间，用于超时退出判断
    last_receive_time = time.time()
    max_idle_time = 5  # 如果超过 max_idle_time 秒没有新数据包，则认为传输结束

    print("开始接收数据包...")

    while True:
        try:
            packet_data, addr = sock.recvfrom(4096)
            last_receive_time = time.time()  # 更新最近接收时间

            # 判断是否为结束信号 "END"
            try:
                if packet_data.decode('utf-8', errors='replace') == "END":
                    print("收到结束信号 'END'")
                    break
            except Exception:
                pass

            # 解析数据包：提取序号、总包数和数据内容
            seq, total, content = parse_packet(packet_data)
            if seq is None:
                continue

            # 记录总包数（首次收到后确定）
            if expected_total is None:
                expected_total = total
                print(f"本次传输总包数：{expected_total}")
            if first_packet_time is None:
                first_packet_time = time.time()
            last_packet_time = time.time()

            # 仅接受与期望一致的连续数据包
            if seq == expected_seq:
                received_packets[seq] = content
                expected_seq += 1
                print(f"客户端: 按序接收包 {seq}")
            else:
                print(f"客户端: 收到乱序包 {seq}，期望 {expected_seq}，忽略此包，并等待重传")

            # 始终回复上一个连续包的ACK（即 expected_seq - 1）
            ack_msg = f"ACK:{expected_seq - 1}"
            sock.sendto(ack_msg.encode('utf-8'), server_addr)
            total_acks += 1

            # 模拟RTT（示例中设定固定0.05秒）
            rtt_list.append(0.05)
        except socket.timeout:
            # 如果长时间未收到新数据包，则检查是否超出空闲时间
            idle_time = time.time() - last_receive_time
            if idle_time > max_idle_time:
                print("长时间未收到数据包，退出接收循环")
                break
            else:
                print("等待数据包超时，继续监听……")
                continue
        except Exception as e:
            print("错误：", e)
            break

    # 检查是否收到的包数与应接收包数一致，若缺失则提示
    if expected_total is not None and len(received_packets) != expected_total:
        print(f"警告：应收到 {expected_total} 个数据包，实际接收 {len(received_packets)} 个。")
    else:
        print("所有数据包均已收到。")

    # 按照序号重组数据包，并拼接形成完整文件数据
    file_data = b""
    for seq in sorted(received_packets.keys()):
        file_data += received_packets[seq]
    local_filename = "received_" + filename
    with open(local_filename, "wb") as f:
        f.write(file_data)
    print(f"文件已保存为 {local_filename}")

    # 统计性能指标
    if first_packet_time and last_packet_time:
        transmission_time = last_packet_time - first_packet_time
    else:
        transmission_time = 0.001
    throughput = file_size / transmission_time if transmission_time > 0 else 0.0
    avg_rtt = sum(rtt_list) / len(rtt_list) if rtt_list else 0

    # 简单估算丢包率：依据总ACK数与预期包数之差计算
    total_packets = expected_total if expected_total else len(received_packets)
    actual_send = total_packets + (total_acks - total_packets)
    packet_loss_rate = ((total_acks - total_packets) / actual_send * 100) if actual_send > 0 else 0

    # 模拟重传次数（这里只作简单估计）
    retransmissions = total_acks-total_packets
    efficiency = file_size / (file_size + retransmissions * PACKET_SIZE)

    print("\n----- 性能指标 -----")
    print(f"传输时间: {transmission_time:.3f} 秒")
    print(f"吞吐量: {throughput:.2f} 字节/秒")
    print(f"平均RTT: {avg_rtt:.3f} 秒")
    print(f"丢包率: {packet_loss_rate:.2f}%")
    print(f"重传次数: {retransmissions}")
    print(f"传输效率: {efficiency*100:.2f}%")

if __name__ == "__main__":
    main()

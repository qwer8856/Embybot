#!/bin/bash
# TelegramEmbyBot 启动脚本 - Linux 版本

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

echo -e "${CYAN}========================================"
echo -e "  TelegramEmbyBot 启动脚本"
echo -e "========================================${NC}\n"

# 切换到脚本所在目录
cd "$(dirname "$0")"

pid_file="bot.pid"

# 检查是否已经在运行
if [ -f "$pid_file" ]; then
    old_pid=$(cat "$pid_file" 2>/dev/null)
    if [ -n "$old_pid" ] && kill -0 "$old_pid" 2>/dev/null; then
        echo -e "${YELLOW}[!] 机器人似乎已在运行 (PID: $old_pid)${NC}"
        echo -e "\n是否强制重启? (y/n)"
        read -r response
        if [[ "$response" =~ ^[Yy]$ ]]; then
            echo -e "${YELLOW}[√] 停止旧进程...${NC}"
            kill "$old_pid" 2>/dev/null || true
            sleep 2
            # 强制终止（如果还在运行）
            if kill -0 "$old_pid" 2>/dev/null; then
                kill -9 "$old_pid" 2>/dev/null || true
            fi
        else
            echo -e "${RED}[×] 已取消启动${NC}"
            exit 0
        fi
    fi
fi

# 确定 Python 命令
python_cmd=""

if [ -f "venv/bin/python3" ]; then
    python_cmd="venv/bin/python3"
    echo -e "${GREEN}[√] 使用虚拟环境: $python_cmd${NC}"
elif [ -f "venv/bin/python" ]; then
    python_cmd="venv/bin/python"
    echo -e "${GREEN}[√] 使用虚拟环境: $python_cmd${NC}"
elif command -v python3 &> /dev/null; then
    python_cmd="python3"
    echo -e "${YELLOW}[!] 使用系统 Python3${NC}"
elif command -v python &> /dev/null; then
    python_cmd="python"
    echo -e "${YELLOW}[!] 使用系统 Python${NC}"
else
    echo -e "${RED}[×] 错误: 未找到 Python 解释器！${NC}"
    exit 1
fi

# 启动机器人
echo -e "\n${GREEN}[√] 启动 TelegramEmbyBot...${NC}\n"

# 确保日志文件存在
touch bot.log

# 使用 nohup 在后台启动
nohup $python_cmd main.py > bot.log 2>&1 &
new_pid=$!

# 保存 PID
echo "$new_pid" > "$pid_file"

# 等待并验证进程
sleep 3

if ps -p "$new_pid" >/dev/null 2>&1; then
    echo -e "${GREEN}[√] 机器人已启动成功！${NC}"
    echo -e "${CYAN}[√] 进程 PID: $new_pid${NC}"
    echo -e "\n${BLUE}日志文件: bot.log${NC}"
    echo -e "${BLUE}查看实时日志: ${YELLOW}tail -f bot.log${NC}"
    echo -e "${BLUE}停止机器人: ${YELLOW}./stop_bot.sh${NC}"
else
    echo -e "${RED}[×] 启动失败，请检查 bot.log 获取错误信息${NC}\n"
    echo -e "${YELLOW}=== bot.log 最后 20 行 ===${NC}"
    tail -n 20 bot.log 2>/dev/null || echo "日志文件为空"
    exit 1
fi

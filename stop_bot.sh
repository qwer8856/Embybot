#!/bin/bash
# TelegramEmbyBot 停止脚本 - Linux 版本

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

echo -e "${CYAN}========================================"
echo -e "  TelegramEmbyBot 停止脚本"
echo -e "========================================${NC}\n"

# 切换到脚本所在目录
cd "$(dirname "$0")"

pid_file="bot.pid"
stopped=0

# 从 PID 文件停止
if [ -f "$pid_file" ]; then
    old_pid=$(cat "$pid_file" 2>/dev/null)
    if [ -n "$old_pid" ]; then
        echo -e "${BLUE}[√] PID 文件中的进程: $old_pid${NC}"
        
        if kill -0 "$old_pid" 2>/dev/null; then
            echo -e "${YELLOW}[√] 停止进程 PID $old_pid...${NC}"
            kill "$old_pid" 2>/dev/null || true
            
            # 等待进程退出（最多5秒）
            for i in {1..5}; do
                if ! kill -0 "$old_pid" 2>/dev/null; then
                    echo -e "${GREEN}[√] 进程已正常退出${NC}"
                    stopped=1
                    break
                fi
                sleep 1
            done
            
            # 如果还在运行，强制终止
            if kill -0 "$old_pid" 2>/dev/null; then
                echo -e "${YELLOW}[!] 进程未响应，执行强制终止...${NC}"
                kill -9 "$old_pid" 2>/dev/null || true
                stopped=1
            fi
        else
            echo -e "${YELLOW}[!] PID 文件中的进程已不存在${NC}"
        fi
    fi
    
    # 删除 PID 文件
    rm -f "$pid_file"
fi

# 查找并停止所有相关进程
echo -e "${BLUE}[√] 查找所有运行 main.py 的进程...${NC}"
found_any=0

while IFS= read -r pid; do
    if [ -n "$pid" ]; then
        echo -e "${YELLOW}[√] 发现并停止进程 PID $pid...${NC}"
        kill "$pid" 2>/dev/null || true
        sleep 1
        # 强制终止（如果还在运行）
        if kill -0 "$pid" 2>/dev/null; then
            kill -9 "$pid" 2>/dev/null || true
        fi
        stopped=1
        found_any=1
    fi
done < <(pgrep -f "python.*main.py" 2>/dev/null || true)

if [ $found_any -eq 0 ] && [ $stopped -eq 0 ]; then
    echo -e "${YELLOW}[!] 未发现运行中的 TelegramEmbyBot 进程${NC}"
fi

echo ""
if [ $stopped -eq 1 ]; then
    echo -e "${GREEN}[√] TelegramEmbyBot 已停止${NC}"
else
    echo -e "${YELLOW}[!] 没有进程被停止${NC}"
fi
echo ""

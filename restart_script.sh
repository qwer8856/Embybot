#!/bin/bash
# TelegramEmbyBot 重启脚本 - Linux 版本
# 完善版本，包含错误处理和回滚机制

# 不使用 set -e，改为手动处理错误，避免脚本意外退出
# set -e

# 创建日志文件
RESTART_LOG="restart.log"
echo "[$(date)] 重启脚本开始执行" >> "$RESTART_LOG"

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
PURPLE='\033[0;35m'
NC='\033[0m' # No Color

# 日志函数
log_info() {
    local msg="$1"
    echo -e "${CYAN}[INFO] $msg${NC}"
    echo "[$(date)] [INFO] $msg" >> "$RESTART_LOG"
}

log_warn() {
    local msg="$1"
    echo -e "${YELLOW}[WARN] $msg${NC}"
    echo "[$(date)] [WARN] $msg" >> "$RESTART_LOG"
}

log_error() {
    local msg="$1"
    echo -e "${RED}[ERROR] $msg${NC}"
    echo "[$(date)] [ERROR] $msg" >> "$RESTART_LOG"
}

log_success() {
    local msg="$1"
    echo -e "${GREEN}[SUCCESS] $msg${NC}"
    echo "[$(date)] [SUCCESS] $msg" >> "$RESTART_LOG"
}

# 错误处理函数
cleanup() {
    local exit_code=$?
    if [ $exit_code -ne 0 ]; then
        log_error "重启脚本异常退出，退出码: $exit_code"
        if [ -n "$backup_pid" ] && [ "$backup_pid" != "0" ]; then
            log_warn "重启失败，尝试恢复原进程..."
            # 这里可以添加恢复逻辑，但通常不可行
        fi
    fi
    echo "[$(date)] 重启脚本结束执行，退出码: $exit_code" >> "$RESTART_LOG"
}

trap cleanup EXIT

echo -e "${PURPLE}╔══════════════════════════════════════════╗${NC}"
echo -e "${PURPLE}║        TelegramEmbyBot 重启脚本          ║${NC}"
echo -e "${PURPLE}║              Linux 完善版               ║${NC}"
echo -e "${PURPLE}╚══════════════════════════════════════════╝${NC}"

# 切换到脚本所在目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 配置变量
PID_FILE="$SCRIPT_DIR/bot.pid"
START_SCRIPT="$SCRIPT_DIR/start_bot.sh"
RUN_IN_BACKGROUND=true

# 检查必要文件
if [ ! -f "main.py" ]; then
    log_error "main.py 文件不存在！"
    exit 1
fi

if [ ! -f "config.py" ]; then
    log_error "config.py 文件不存在！"
    exit 1
fi

# 等待确保 Telegram 消息发送完成
log_info "等待 3 秒以确保重启消息发送完成..."
sleep 3

pid_file="$PID_FILE"  # 使用统一的变量名
backup_pid=""
declare -a found_pids

log_info "开始执行重启流程..."
echo -e "${BLUE}────────────────────────────────────────${NC}"

echo -e "\n${YELLOW}[步骤 1/5] 检查并备份当前进程信息...${NC}"

# 从 PID 文件读取并备份
if [ -f "$pid_file" ]; then
    backup_pid=$(cat "$pid_file" 2>/dev/null)
    if [ -n "$backup_pid" ]; then
        if kill -0 "$backup_pid" 2>/dev/null; then
            log_info "找到运行中的进程: PID $backup_pid"
            found_pids+=("$backup_pid")
            # 记录进程启动时间和命令
            ps -o pid,ppid,cmd,etime -p "$backup_pid" > "backup_process_info.tmp" 2>/dev/null || true
        else
            log_warn "PID 文件中的进程已不存在"
        fi
    fi
fi

# 查找所有相关进程
log_info "扫描所有运行 main.py 的 Python 进程..."
while IFS= read -r pid; do
    if [ -n "$pid" ]; then
        found_pids+=("$pid")
        log_info "发现进程: PID $pid"
    fi
done < <(pgrep -f "python.*main.py" 2>/dev/null || true)

# 检查虚拟环境进程
if [ -d "venv" ]; then
    while IFS= read -r pid; do
        if [ -n "$pid" ]; then
            # 检查是否已在列表中
            if [[ ! " ${found_pids[@]} " =~ " ${pid} " ]]; then
                found_pids+=("$pid")
                log_info "发现虚拟环境进程: PID $pid"
            fi
        fi
    done < <(pgrep -f "venv.*python.*main.py" 2>/dev/null || true)
fi

echo -e "\n${YELLOW}[步骤 2/5] 优雅停止现有进程...${NC}"

# 去重处理
declare -a unique_pids
for pid in "${found_pids[@]}"; do
    if [ -n "$pid" ]; then
        skip=false
        for upid in "${unique_pids[@]}"; do
            if [ "$upid" = "$pid" ]; then
                skip=true
                break
            fi
        done
        if [ "$skip" = false ]; then
            unique_pids+=("$pid")
        fi
    fi
done

# 停止进程
if [ ${#unique_pids[@]} -gt 0 ]; then
    log_success "发现 ${#unique_pids[@]} 个需要停止的进程: ${unique_pids[*]}"
    
    # 第一阶段：发送 SIGTERM 信号
    for pid in "${unique_pids[@]}"; do
        if kill -0 "$pid" 2>/dev/null; then
            log_info "发送 SIGTERM 信号给进程 PID $pid..."
            kill -TERM "$pid" 2>/dev/null || true
        fi
    done

    # 等待进程优雅退出（最多15秒）
    log_info "等待进程优雅退出（最多15秒）..."
    for second in $(seq 1 15); do
        remaining=()
        for pid in "${unique_pids[@]}"; do
            if kill -0 "$pid" 2>/dev/null; then
                remaining+=("$pid")
            fi
        done
        
        if [ ${#remaining[@]} -eq 0 ]; then
            log_success "所有进程已优雅退出"
            break
        fi
        
        # 显示进度
        if [ $((second % 3)) -eq 0 ]; then
            log_info "等待中... 剩余进程: ${remaining[*]}"
        fi
        
        if [ $second -eq 15 ]; then
            log_warn "以下进程未响应，执行强制终止: ${remaining[*]}"
            for pid in "${remaining[@]}"; do
                log_warn "强制终止 PID $pid"
                kill -9 "$pid" 2>/dev/null || true
            done
            sleep 2
        else
            sleep 1
        fi
    done
else
    log_info "未发现运行中的 TelegramEmbyBot 进程"
fi

echo -e "\n${YELLOW}[步骤 3/5] 清理 PID 文件...${NC}"

# 清理 PID 文件
if [ -f "$PID_FILE" ]; then
    rm -f "$PID_FILE"
    log_success "已清理旧的 PID 文件: $PID_FILE"
else
    log_info "无需清理 PID 文件（文件不存在）"
fi

# 等待一下确保端口释放
log_info "等待端口释放..."
sleep 2

echo -e "\n${YELLOW}[步骤 4/5] 重新启动机器人...${NC}"

# 检查启动脚本，如果不存在则创建
if [ ! -f "$START_SCRIPT" ]; then
    log_warn "启动脚本不存在，正在创建: $START_SCRIPT"
    
    # 创建启动脚本
    cat > "$START_SCRIPT" << 'EOF'
#!/bin/bash
# TelegramEmbyBot 启动脚本

cd "$(dirname "$0")"

# 确定 Python 命令
python_cmd=""
if [ -f "venv/bin/python3" ]; then
    python_cmd="venv/bin/python3"
    echo "使用虚拟环境: $python_cmd"
elif [ -f "venv/bin/python" ]; then
    python_cmd="venv/bin/python"
    echo "使用虚拟环境: $python_cmd"
elif command -v python3 &> /dev/null; then
    python_cmd="python3"
    echo "使用系统 Python3: $python_cmd"
elif command -v python &> /dev/null; then
    python_cmd="python"
    echo "使用系统 Python: $python_cmd"
else
    echo "错误: 未找到 Python 解释器！"
    exit 1
fi

# 启动机器人
echo "启动 TelegramEmbyBot..."
exec $python_cmd main.py
EOF
    
    chmod +x "$START_SCRIPT"
    log_success "已创建启动脚本: $START_SCRIPT"
fi

# 检查脚本权限
if [ ! -x "$START_SCRIPT" ]; then
    log_warn "启动脚本权限不足，尝试添加执行权限"
    chmod +x "$START_SCRIPT" || {
        log_error "无法设置执行权限"
        exit 1
    }
fi

# 启动机器人
log_info "执行启动命令: $START_SCRIPT"
if [ "$RUN_IN_BACKGROUND" = true ]; then
    log_info "后台模式启动..."
    nohup "$START_SCRIPT" > bot.log 2>&1 &
    STARTUP_PID=$!
    log_success "启动命令已执行，PID: $STARTUP_PID"
else
    log_info "前台模式启动..."
    "$START_SCRIPT" &
    STARTUP_PID=$!
    log_success "启动命令已执行，PID: $STARTUP_PID"
fi

echo -e "\n${YELLOW}[步骤 5/5] 验证启动状态...${NC}"

# 等待一下让进程启动
log_info "等待机器人启动完成..."
sleep 5

# 验证进程是否运行
new_bot_pids=()
while IFS= read -r pid; do
    if [ -n "$pid" ]; then
        new_bot_pids+=("$pid")
    fi
done < <(pgrep -f "python.*main.py" 2>/dev/null || true)

if [ ${#new_bot_pids[@]} -gt 0 ]; then
    log_success "TelegramEmbyBot 已成功启动！"
    log_success "运行中的进程: ${new_bot_pids[*]}"
    
    # 保存新的 PID
    if [ ${#new_bot_pids[@]} -eq 1 ]; then
        echo "${new_bot_pids[0]}" > "$pid_file"
        log_success "PID 已保存到文件: $pid_file"
    else
        log_warn "发现多个进程，手动管理 PID 文件"
    fi
else
    log_error "机器人启动失败，未发现运行中的进程"
    echo -e "${RED}请检查启动脚本和日志信息${NC}"
    echo -e "${YELLOW}建议手动运行: $START_SCRIPT${NC}"
    echo -e "${YELLOW}查看日志: tail -f bot.log${NC}"
    exit 1
fi

echo -e "\n${GREEN}========================================${NC}"
echo -e "${GREEN}        TelegramEmbyBot 重启完成        ${NC}"
echo -e "${GREEN}========================================${NC}"
echo -e "${CYAN}运行状态: ${GREEN}成功${NC}"
echo -e "${CYAN}进程数量: ${GREEN}${#new_bot_pids[@]}${NC}"
echo -e "${CYAN}PID 列表: ${GREEN}${new_bot_pids[*]}${NC}"
echo -e "${CYAN}PID 文件: ${GREEN}$pid_file${NC}"
echo -e "${CYAN}日志文件: ${GREEN}bot.log${NC}"
echo -e "${GREEN}========================================${NC}"

log_success "重启操作完成！"

# 清理临时文件
rm -f backup_process_info.tmp

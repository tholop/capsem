# Capsem profile shell bootstrap.
export PATH="/opt/ai-clis/bin:/usr/local/bin:/root/.local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export UV_PYTHON_INSTALL_DIR="/opt/uv/python"
export UV_TOOL_DIR="/opt/uv/tools"
export UV_TOOL_BIN_DIR="/usr/local/bin"
export UV_NATIVE_TLS=1

if [ ! -e /root/terminal-bench ] && [ -d /opt/terminal-bench ]; then
    ln -s /opt/terminal-bench /root/terminal-bench 2>/dev/null || true
fi

if [ -x /usr/sbin/dockerd ] && ! pgrep -x dockerd >/dev/null 2>&1; then
    nohup /usr/sbin/dockerd -p /var/run/docker.pid >/var/log/docker.log 2>&1 &
fi

if [ -f /root/tips.txt ]; then
    sed -n '1,3p' /root/tips.txt
fi

# Capsem profile login shell bootstrap.
export PATH="/opt/ai-clis/bin:/usr/local/bin:/root/.local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export UV_PYTHON_INSTALL_DIR="/opt/uv/python"
export UV_TOOL_DIR="/opt/uv/tools"
export UV_TOOL_BIN_DIR="/usr/local/bin"
export UV_NATIVE_TLS=1

if [ -f /root/.bashrc ]; then
    . /root/.bashrc
fi

# Arches ISO — launch installer on tty1, recovery shell on other ttys
if [[ "$(tty)" == "/dev/tty1" ]]; then
    arches-install
    # If the installer exits (e.g. "Recovery Shell"), drop to zsh
    echo ""
    echo "  Recovery shell. Run 'arches-install' to restart the installer."
    echo ""
    exec /bin/zsh
fi

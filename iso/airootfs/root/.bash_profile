# Arches ISO — auto-launch installer or drop to recovery shell

cat << 'BANNER'

    ╔═══════════════════════════════════════╗
    ║           A R C H E S                 ║
    ║   Arch/CachyOS Install & Recovery     ║
    ╚═══════════════════════════════════════╝

BANNER

echo "  [1] Launch Installer"
echo "  [2] Recovery Shell"
echo ""
read -rp "  Select [1/2]: " choice

case "$choice" in
    2)
        echo ""
        echo "  Dropping to recovery shell. The installer is available at:"
        echo "    arches-install"
        echo ""
        exec /bin/zsh
        ;;
    *)
        exec arches-install
        ;;
esac

#!/usr/bin/env bash
#
# Build mainline llama.cpp from git. Up to three variants are produced
# depending on the ARCHES_GPU build flag:
#
#   - llama.cpp-vulkan-arches  Vulkan backend (works on all RDNA, Intel
#                              Arc, and NVIDIA via Mesa). Default.
#   - llama.cpp-hip-arches     AMD ROCm/HIP backend (gfx1201 fat-binary).
#                              Built when amd-rocm is in the stack list.
#   - llama.cpp-cuda-arches    NVIDIA CUDA backend. Built when nvidia-cuda
#                              is in the stack list.
#
# All three packages provide `llama.cpp` and conflict with each other
# and with the AUR `llama.cpp*` family — only one may be installed at
# a time. Their corresponding modules declare matching [dependencies]
# .conflicts so the installer enforces this.
#
# Which variants we build is driven by ARCHES_GPU (set by the higher
# build pipeline). If unset, default to building all three so a build
# without explicit GPU selection still gets a complete set. Skipping
# unused variants saves significant build time — the HIP build pulls
# the ROCm toolchain (~3-4 GB) and the CUDA build pulls the CUDA
# toolkit (~4 GB).
#
# Cache key: upstream HEAD commit + variant set + AMDGPU_TARGETS
# + CUDA_ARCHITECTURES. Re-run with FORCE=true to rebuild even when
# the SHA and config are unchanged.
#
# Called by scripts/build-aur-repo.sh. Receives:
#   REPO_DIR               directory to copy built .pkg.tar.* files into
#   BUILD_DIR              scratch directory for builds
#   PLATFORM               target platform (e.g. x86-64)
#   FORCE                  "true" to rebuild regardless of cache
#   ARCHES_BUILD_CACHE_LIB path to the build cache helper library
#   ARCHES_GPU             optional, comma-separated GPU stack list
#                          (amd-vulkan, amd-rocm, intel-vulkan, nvidia-cuda)
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

: "${REPO_DIR:?REPO_DIR must be set}"
: "${BUILD_DIR:?BUILD_DIR must be set}"
: "${FORCE:=false}"

# ── Build cache helpers ───────────────────────────────
if [[ -n "${ARCHES_BUILD_CACHE_LIB:-}" && -f "$ARCHES_BUILD_CACHE_LIB" ]]; then
    source "$ARCHES_BUILD_CACHE_LIB"
else
    source "$PROJECT_ROOT/scripts/lib/build-cache.sh"
fi

# ── Configuration ─────────────────────────────────────
UPSTREAM_REPO="https://github.com/ggml-org/llama.cpp"
UPSTREAM_BRANCH="master"

# AMD GPU targets for the HIP build. gfx1201 = Radeon AI PRO R9700 /
# RX 9070 (RDNA4). gfx1100/1101/1102 = RX 7900/7800/7700 (RDNA3).
# gfx1030 = RX 6800/6900 (RDNA2). Built as a fat binary so the same
# package works on the workstation and on dev machines with older AMD
# GPUs. Override via AMDGPU_TARGETS env var.
AMDGPU_TARGETS_DEFAULT="gfx1201;gfx1100;gfx1101;gfx1102;gfx1030"
AMDGPU_TARGETS="${AMDGPU_TARGETS:-$AMDGPU_TARGETS_DEFAULT}"

# NVIDIA CUDA architectures (SM versions). Defaults cover the
# commonly-used range: Pascal (60) through Ada Lovelace (89). Hopper
# (90, datacenter) and Blackwell (100+) excluded by default since
# they're rare in workstations and add build time. Override via
# CUDA_ARCHITECTURES env var (semicolon-separated).
#
# Reference:
#   60 = GP100 (P100)
#   61 = GP102/104/106 (GTX 1080/1070/1060)
#   70 = GV100 (V100)
#   75 = TU102/104/106/116/117 (RTX 2080, 1660)
#   80 = GA100 (A100)
#   86 = GA102/104/106/107 (RTX 3090/3080/3070/3060)
#   89 = AD102/103/104/106/107 (RTX 4090/4080/4070/4060)
#   90 = GH100 (H100, datacenter)
#  100 = GB100/202 (RTX 5090/5080 - Blackwell)
CUDA_ARCHITECTURES_DEFAULT="60;70;75;80;86;89"
CUDA_ARCHITECTURES="${CUDA_ARCHITECTURES:-$CUDA_ARCHITECTURES_DEFAULT}"

PKG_VULKAN="llama.cpp-vulkan-arches"
PKG_HIP="llama.cpp-hip-arches"
PKG_CUDA="llama.cpp-cuda-arches"

# ── Variant selection from ARCHES_GPU ─────────────────
# Default (no ARCHES_GPU set): build all three variants. This matches
# the "no filter" case for the rest of the build pipeline.
#
# When ARCHES_GPU is set, build only the variants needed by the
# selected stacks. Multi-stack builds may need multiple variants
# (e.g. amd-vulkan,nvidia-cuda → vulkan + cuda).
BUILD_VULKAN=false
BUILD_HIP=false
BUILD_CUDA=false

if [[ -z "${ARCHES_GPU:-}" ]]; then
    # Unset: build all variants (legacy default for non-filtered builds).
    BUILD_VULKAN=true
    BUILD_HIP=true
    # CUDA is OFF by default even with no filter, because the CUDA
    # toolkit is not pre-installed in the build container. Operators
    # who want CUDA must opt in via ARCHES_GPU=nvidia-cuda (or include
    # it explicitly via ARCHES_GPU=...nvidia-cuda... ).
    BUILD_CUDA=false
else
    # Parse comma/whitespace-separated stack list.
    _stacks=$(echo "$ARCHES_GPU" | tr ',' ' ')
    for stack in $_stacks; do
        case "$stack" in
            amd-vulkan|intel-vulkan)
                BUILD_VULKAN=true
                ;;
            amd-rocm)
                BUILD_HIP=true
                # amd-rocm + amd-vulkan in same build = arbitration says
                # Vulkan wins, but we still build both packages so the
                # operator can swap at install time.
                ;;
            nvidia-cuda)
                BUILD_CUDA=true
                ;;
            *)
                echo "WARNING: llama.cpp build.sh: unknown GPU stack '$stack' in ARCHES_GPU — ignoring" >&2
                ;;
        esac
    done
fi

echo "── llama.cpp variant selection (ARCHES_GPU='${ARCHES_GPU:-}') ──"
echo "    Vulkan: $BUILD_VULKAN"
echo "    HIP:    $BUILD_HIP"
echo "    CUDA:   $BUILD_CUDA"

if [[ "$BUILD_VULKAN" == false && "$BUILD_HIP" == false && "$BUILD_CUDA" == false ]]; then
    echo "── No llama.cpp variants requested — skipping. ──"
    exit 0
fi

# ── CUDA toolchain availability check ────────────────
if [[ "$BUILD_CUDA" == true ]]; then
    if ! command -v nvcc &>/dev/null && ! [[ -x /opt/cuda/bin/nvcc ]]; then
        echo "ERROR: CUDA toolkit not available in this build container." >&2
        echo "       Install 'cuda' in the Containerfile or use a builder image" >&2
        echo "       with the CUDA toolkit pre-installed. To skip the CUDA" >&2
        echo "       variant, remove 'nvidia-cuda' from ARCHES_GPU." >&2
        exit 1
    fi
fi

# --------------------------------------------------------------------------
# 1. Resolve upstream HEAD SHA — used both as cache key and pkgver
# --------------------------------------------------------------------------

echo "── Resolving upstream HEAD: $UPSTREAM_REPO@$UPSTREAM_BRANCH ──"
UPSTREAM_SHA=$(git ls-remote "$UPSTREAM_REPO" "refs/heads/$UPSTREAM_BRANCH" \
               | awk '{print $1}')
if [[ -z "$UPSTREAM_SHA" ]]; then
    echo "ERROR: failed to resolve $UPSTREAM_REPO@$UPSTREAM_BRANCH" >&2
    exit 1
fi
SHORT_SHA="${UPSTREAM_SHA:0:10}"
echo "  HEAD: $UPSTREAM_SHA"

PKGVER="$(date -u +%Y%m%d).${SHORT_SHA}"

# Cache hash includes the SHA, both target lists, this script, AND
# the variant-selection booleans (changing what we build invalidates).
CACHE_HASH=$(compute_cache_hash \
    "$SCRIPT_DIR/build.sh" \
    "str:sha=$UPSTREAM_SHA" \
    "str:amd_targets=$AMDGPU_TARGETS" \
    "str:cuda_archs=$CUDA_ARCHITECTURES" \
    "str:variants=v=${BUILD_VULKAN},h=${BUILD_HIP},c=${BUILD_CUDA}")

# --------------------------------------------------------------------------
# 2. Cache check — skip if every requested variant is cached
# --------------------------------------------------------------------------

all_cached=true
if [[ "$FORCE" == false ]]; then
    if [[ "$BUILD_VULKAN" == true ]] && ! pkg_cache_hit "$REPO_DIR" "$PKG_VULKAN" "$CACHE_HASH"; then
        all_cached=false
    fi
    if [[ "$BUILD_HIP" == true ]] && ! pkg_cache_hit "$REPO_DIR" "$PKG_HIP" "$CACHE_HASH"; then
        all_cached=false
    fi
    if [[ "$BUILD_CUDA" == true ]] && ! pkg_cache_hit "$REPO_DIR" "$PKG_CUDA" "$CACHE_HASH"; then
        all_cached=false
    fi
else
    all_cached=false
fi

if [[ "$all_cached" == true ]]; then
    echo "── llama.cpp: requested variants all cached at $SHORT_SHA, skipping ──"
    exit 0
fi

# Remove stale .pkg.tar.* files for whichever variants we're about to build
[[ "$BUILD_VULKAN" == true ]] && remove_stale_packages "$REPO_DIR" "$PKG_VULKAN"
[[ "$BUILD_HIP" == true ]]    && remove_stale_packages "$REPO_DIR" "$PKG_HIP"
[[ "$BUILD_CUDA" == true ]]   && remove_stale_packages "$REPO_DIR" "$PKG_CUDA"

# --------------------------------------------------------------------------
# 3. Generate PKGBUILD (split package: only requested variants)
# --------------------------------------------------------------------------

PKG_BUILD_DIR="$BUILD_DIR/llama.cpp-arches"
rm -rf "$PKG_BUILD_DIR"
mkdir -p "$PKG_BUILD_DIR"

# Build the pkgname list dynamically based on selected variants.
_pkgname_list=""
[[ "$BUILD_VULKAN" == true ]] && _pkgname_list="$_pkgname_list '$PKG_VULKAN'"
[[ "$BUILD_HIP" == true ]]    && _pkgname_list="$_pkgname_list '$PKG_HIP'"
[[ "$BUILD_CUDA" == true ]]   && _pkgname_list="$_pkgname_list '$PKG_CUDA'"

# Per-variant makedepends (combined into the single makedepends list).
_makedeps_common="'cmake' 'ninja' 'git' 'gcc' 'python' 'openblas' 'openssl'"
_makedeps_vulkan="'vulkan-headers' 'vulkan-icd-loader' 'shaderc' 'spirv-headers'"
_makedeps_hip="'rocm-hip-sdk' 'rocm-cmake' 'rocwmma'"
_makedeps_cuda="'cuda' 'cuda-tools'"

_makedeps="$_makedeps_common"
[[ "$BUILD_VULKAN" == true ]] && _makedeps="$_makedeps $_makedeps_vulkan"
[[ "$BUILD_HIP" == true ]]    && _makedeps="$_makedeps $_makedeps_hip"
[[ "$BUILD_CUDA" == true ]]   && _makedeps="$_makedeps $_makedeps_cuda"

# Build-step shell snippets for each variant. Generated conditionally
# so the PKGBUILD doesn't include build commands for variants we
# didn't request (which would otherwise fail when toolchains are absent).
_build_vulkan=""
if [[ "$BUILD_VULKAN" == true ]]; then
    read -r -d '' _build_vulkan <<VULKAN_EOF || true
    # ── Vulkan build ──
    cmake -S . -B build-vulkan "\${_cmake_common[@]}" \\
        -DGGML_VULKAN=ON \\
        -DGGML_HIP=OFF \\
        -DGGML_CUDA=OFF
    cmake --build build-vulkan
VULKAN_EOF
fi

_build_hip=""
if [[ "$BUILD_HIP" == true ]]; then
    read -r -d '' _build_hip <<HIP_EOF || true
    # ── HIP build ──
    # Use ROCm's clang as the C/C++ compiler so HIP device code compiles.
    export ROCM_PATH=/opt/rocm
    export HIP_PATH=/opt/rocm
    cmake -S . -B build-hip "\${_cmake_common[@]}" \\
        -DCMAKE_C_COMPILER=/opt/rocm/llvm/bin/clang \\
        -DCMAKE_CXX_COMPILER=/opt/rocm/llvm/bin/clang++ \\
        -DGGML_VULKAN=OFF \\
        -DGGML_HIP=ON \\
        -DGGML_HIP_ROCWMMA_FATTN=ON \\
        -DGGML_CUDA=OFF \\
        -DAMDGPU_TARGETS='$AMDGPU_TARGETS'
    cmake --build build-hip
HIP_EOF
fi

_build_cuda=""
if [[ "$BUILD_CUDA" == true ]]; then
    read -r -d '' _build_cuda <<CUDA_EOF || true
    # ── CUDA build ──
    # CUDA toolkit lives at /opt/cuda on Arch (the 'cuda' package).
    export CUDACXX=/opt/cuda/bin/nvcc
    export PATH="/opt/cuda/bin:\$PATH"
    cmake -S . -B build-cuda "\${_cmake_common[@]}" \\
        -DGGML_VULKAN=OFF \\
        -DGGML_HIP=OFF \\
        -DGGML_CUDA=ON \\
        -DCMAKE_CUDA_ARCHITECTURES='$CUDA_ARCHITECTURES'
    cmake --build build-cuda
CUDA_EOF
fi

# Per-variant package() functions. Same pattern: only emit the ones
# we built.
_package_vulkan=""
if [[ "$BUILD_VULKAN" == true ]]; then
    read -r -d '' _package_vulkan <<PVULKAN_EOF || true
package_${PKG_VULKAN}() {
    pkgdesc='llama.cpp built from mainline git with Vulkan GPU backend (Arches)'
    depends=(
        'glibc' 'gcc-libs' 'openblas' 'openssl'
        'vulkan-icd-loader'
    )
    optdepends=(
        'vulkan-radeon: AMD GPU Vulkan ICD'
        'nvidia-utils: NVIDIA GPU Vulkan ICD'
        'vulkan-intel: Intel GPU Vulkan ICD'
    )
    provides=('llama.cpp' "llama.cpp=\$pkgver")
    conflicts=('llama.cpp' 'llama.cpp-vulkan' 'llama.cpp-hip'
               'llama.cpp-cuda' 'llama.cpp-bin' 'llama.cpp-git'
               '$PKG_HIP' '$PKG_CUDA')
    _install_variant build-vulkan vulkan
}
PVULKAN_EOF
fi

_package_hip=""
if [[ "$BUILD_HIP" == true ]]; then
    read -r -d '' _package_hip <<PHIP_EOF || true
package_${PKG_HIP}() {
    pkgdesc='llama.cpp built from mainline git with AMD ROCm/HIP backend (Arches, gfx1201/RDNA4)'
    depends=(
        'glibc' 'gcc-libs' 'openblas' 'openssl'
        'rocm-hip-runtime' 'hipblas' 'rocblas'
    )
    provides=('llama.cpp' "llama.cpp=\$pkgver")
    conflicts=('llama.cpp' 'llama.cpp-vulkan' 'llama.cpp-hip'
               'llama.cpp-cuda' 'llama.cpp-bin' 'llama.cpp-git'
               '$PKG_VULKAN' '$PKG_CUDA')
    _install_variant build-hip hip
}
PHIP_EOF
fi

_package_cuda=""
if [[ "$BUILD_CUDA" == true ]]; then
    read -r -d '' _package_cuda <<PCUDA_EOF || true
package_${PKG_CUDA}() {
    pkgdesc='llama.cpp built from mainline git with NVIDIA CUDA backend (Arches)'
    depends=(
        'glibc' 'gcc-libs' 'openblas' 'openssl'
        'cuda' 'nvidia-utils'
    )
    provides=('llama.cpp' "llama.cpp=\$pkgver")
    conflicts=('llama.cpp' 'llama.cpp-vulkan' 'llama.cpp-hip'
               'llama.cpp-cuda' 'llama.cpp-bin' 'llama.cpp-git'
               '$PKG_VULKAN' '$PKG_HIP')
    _install_variant build-cuda cuda
}
PCUDA_EOF
fi

cat > "$PKG_BUILD_DIR/PKGBUILD" <<PKGEOF
# Auto-generated by modules/llama-cpp/build.sh — do not edit.
# Variants selected by ARCHES_GPU at generation time:
#   Vulkan: $BUILD_VULKAN
#   HIP:    $BUILD_HIP
#   CUDA:   $BUILD_CUDA

pkgbase=llama.cpp-arches
pkgname=($_pkgname_list)
pkgver=$PKGVER
pkgrel=1
arch=('x86_64')
url='https://github.com/ggml-org/llama.cpp'
license=('MIT')

makedepends=($_makedeps)

source=("git+${UPSTREAM_REPO}#commit=${UPSTREAM_SHA}")
sha256sums=('SKIP')

_cmake_common=(
    -G Ninja
    -DCMAKE_BUILD_TYPE=Release
    -DCMAKE_INSTALL_PREFIX=/usr
    -DBUILD_SHARED_LIBS=ON
    -DLLAMA_BUILD_TESTS=OFF
    -DLLAMA_BUILD_EXAMPLES=ON
    -DLLAMA_BUILD_SERVER=ON
    -DLLAMA_OPENSSL=ON
    -DGGML_LTO=ON
    -DGGML_NATIVE=OFF
    -DGGML_BLAS=ON
    -DGGML_BLAS_VENDOR=OpenBLAS
)

build() {
    cd "\$srcdir/llama.cpp"

$_build_vulkan

$_build_hip

$_build_cuda
}

_install_variant() {
    # \$1 = build dir, \$2 = suffix
    cd "\$srcdir/llama.cpp"
    DESTDIR="\$pkgdir" cmake --install "\$1"
    install -Dm644 LICENSE "\$pkgdir/usr/share/licenses/\$pkgname/LICENSE"
}

$_package_vulkan

$_package_hip

$_package_cuda
PKGEOF

# --------------------------------------------------------------------------
# 4. Build
# --------------------------------------------------------------------------

_variants_built=""
[[ "$BUILD_VULKAN" == true ]] && _variants_built="$_variants_built vulkan"
[[ "$BUILD_HIP" == true ]]    && _variants_built="$_variants_built hip"
[[ "$BUILD_CUDA" == true ]]   && _variants_built="$_variants_built cuda"

echo "── Building llama.cpp $PKGVER (${_variants_built# }) ──"
echo "    SHA:    $UPSTREAM_SHA"
[[ "$BUILD_HIP" == true ]]  && echo "    HIP targets:  $AMDGPU_TARGETS"
[[ "$BUILD_CUDA" == true ]] && echo "    CUDA archs:   $CUDA_ARCHITECTURES"

cd "$PKG_BUILD_DIR"
# --skipchecksums: source is a git commit pin, no tarball checksum
# --skippgpcheck:  no PGP signature on the upstream repo
makepkg -sf --noconfirm --needed --skipchecksums --skippgpcheck

cp ./*.pkg.tar.* "$REPO_DIR/"

# Persist cache hash for each successfully-built variant and emit a
# "Built: <pkg> <ver>" line. Wrapped in `if` blocks rather than the
# `[[ ]] && cmd` short-circuit pattern because under `set -e` a final
# false short-circuit at script tail propagates as a non-zero exit
# code — which makes build-aur-repo.sh think this module failed even
# though every requested variant built cleanly.
if [[ "$BUILD_VULKAN" == true ]]; then
    save_cache_hash "$REPO_DIR" "$PKG_VULKAN" "$CACHE_HASH"
    echo "  Built: $PKG_VULKAN $PKGVER"
fi
if [[ "$BUILD_HIP" == true ]]; then
    save_cache_hash "$REPO_DIR" "$PKG_HIP" "$CACHE_HASH"
    echo "  Built: $PKG_HIP $PKGVER"
fi
if [[ "$BUILD_CUDA" == true ]]; then
    save_cache_hash "$REPO_DIR" "$PKG_CUDA" "$CACHE_HASH"
    echo "  Built: $PKG_CUDA $PKGVER"
fi

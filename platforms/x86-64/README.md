# x86-64 Platform

x86-64 platform using CachyOS optimized packages with Limine bootloader and btrfs subvolumes.

## Kernel Variants

Multiple kernel variants can be declared in `platform.toml` under `[kernel].variants`. Each variant produces a separate bootloader entry. The first variant is the default (used for the ISO boot menu) unless one is explicitly marked `default = true`.

```toml
[kernel]
variants = [
    { package = "linux-cachyos", headers = "linux-cachyos-headers" },
    { package = "linux-cachyos-lts", headers = "linux-cachyos-lts-headers" },
]
```

### Available CachyOS Kernels

| Package                  | Scheduler                               | Best For                          |
|--------------------------|-----------------------------------------|-----------------------------------|
| `linux-cachyos`          | EEVDF (default, GCC + Thin LTO)         | General-purpose computing         |
| `linux-cachyos-bore`     | BORE (Burst-Oriented Response Enhancer) | Interactive workloads and gaming  |
| `linux-cachyos-lts`      | LTS kernel                              | Stability, fallback               |
| `linux-cachyos-rt-bore`  | BORE + RT patches                       | Real-time / low-latency workloads |
| `linux-cachyos-hardened` | Hardened config                         | Security-focused                  |
| `linux-cachyos-server`   | Server-tuned config                     | Server workloads                  |

## CachyOS Optimization Tier

The `cachyos_optimization_tier` field in `platform.toml` controls which CachyOS repository tier is used. This affects the **entire package set** (glibc, mesa, ffmpeg, etc.), not just the kernel. All packages in the tier-specific repos are recompiled with the corresponding compiler flags.

```toml
[platform]
cachyos_optimization_tier = "x86-64-v3"
```

### Available Tiers

| Tier        | Compiler Flags                         | Hardware Requirement                  | Package Coverage                     |
|-------------|----------------------------------------|---------------------------------------|--------------------------------------|
| `x86-64`    | Baseline                               | All x86-64 CPUs                       | Kernels only (from `[cachyos]` repo) |
| `x86-64-v3` | `-march=x86-64-v3` (AVX, AVX2, SSE4.2) | 2011+ (Intel Nehalem / AMD Bulldozer) | Full package set                     |
| `x86-64-v4` | `-march=x86-64-v4` (AVX-512)           | Intel Haswell+ / AMD Zen 4+           | Full package set                     |
| `znver4`    | `-march=znver4` (Zen 4 tuning)         | AMD Ryzen 7000+ (Zen 4/5 only)        | Full package set                     |

### Changing the Optimization Tier

When changing the tier, three files must be updated together:

#### 1. `platform.toml` — Set the tier

```toml
cachyos_optimization_tier = "znver4"
```

#### 2. `pacman.conf` — Configure the matching repos

Each tier uses different repo names and mirrorlist files:

**`x86-64` (baseline):**
```
# Only the base cachyos repo (kernels and tooling)
[cachyos]
Include = /etc/pacman.d/cachyos-mirrorlist
```

**`x86-64-v3`:**
```
[cachyos-v3]
Include = /etc/pacman.d/cachyos-v3-mirrorlist

[cachyos-core-v3]
Include = /etc/pacman.d/cachyos-v3-mirrorlist

[cachyos-extra-v3]
Include = /etc/pacman.d/cachyos-v3-mirrorlist

[cachyos]
Include = /etc/pacman.d/cachyos-mirrorlist
```

**`x86-64-v4`:**
```
[cachyos-v4]
Include = /etc/pacman.d/cachyos-v4-mirrorlist

[cachyos-core-v4]
Include = /etc/pacman.d/cachyos-v4-mirrorlist

[cachyos-extra-v4]
Include = /etc/pacman.d/cachyos-v4-mirrorlist

[cachyos]
Include = /etc/pacman.d/cachyos-mirrorlist
```

**`znver4`** (note: uses the v4 mirrorlist):
```
[cachyos-znver4]
Include = /etc/pacman.d/cachyos-v4-mirrorlist

[cachyos-core-znver4]
Include = /etc/pacman.d/cachyos-v4-mirrorlist

[cachyos-extra-znver4]
Include = /etc/pacman.d/cachyos-v4-mirrorlist

[cachyos]
Include = /etc/pacman.d/cachyos-mirrorlist
```

#### 3. `platform.toml` `[base_packages]` — Update the mirrorlist package

| Tier        | Mirrorlist Package                                           |
|-------------|--------------------------------------------------------------|
| `x86-64`    | `cachyos-mirrorlist` (already included via `[cachyos]` repo) |
| `x86-64-v3` | `cachyos-v3-mirrorlist`                                      |
| `x86-64-v4` | `cachyos-v4-mirrorlist`                                      |
| `znver4`    | `cachyos-v4-mirrorlist`                                      |

### Checking CPU Compatibility

To check which tiers your CPU supports:

```bash
/lib/ld-linux-x86-64.so.2 --help | grep supported
```

Look for `(supported, searched)` next to each microarchitecture level.

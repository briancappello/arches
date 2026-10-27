# arches-local repository key

Design D7 (`docs/superpowers/specs/2026-10-27-secure-spare-slot-install-design.md`).

`[arches-local]` has `SigLevel = Required`: pacman installs a package from it
only if the package and the repository database carry a valid signature from
this key. The private key is generated on the YubiKey and never leaves it.
Only two files live here:

| File | Content |
| --- | --- |
| `arches-local.asc` | The public key, ASCII-armored |
| `arches-local.fpr` | Its fingerprint: 40 hex characters, one line |

Until both exist, ISO builds and installs fail: nothing can verify
arches-local.

## Create the key (operator, once)

On the build host, with the YubiKey inserted:

```bash
gpg --card-edit
#   admin
#   key-attr          # ECC, Curve 25519 for signing
#   generate          # answer "n" to the off-card backup
#   quit
ykman openpgp keys set-touch sig on   # one touch per signature
gpg --list-secret-keys --with-colons | awk -F: '$1=="fpr"{print $10; exit}' > keys/arches-local.fpr
gpg --armor --export "$(cat keys/arches-local.fpr)" > keys/arches-local.asc
```

Check that field 15 of the `sec` or `ssb` record is the card's serial
number, not `+` (a software key):

```bash
gpg --with-colons --list-secret-keys "$(cat keys/arches-local.fpr)"
```

Commit both files through a reviewed change.

## Sign after every build (operator)

```bash
./scripts/build-aur-repo.sh x86-64   # unsigned: agents and the builder may run this
./scripts/sign-local-repo.sh         # one touch per package and one for the database
```

`sign-local-repo.sh` refuses a key that is not on a smartcard. The build
refuses an unsigned repository, so this step is the gate between built code
and an installed system.

## Losing the YubiKey

Create a new key as above, replace both files, and sign again. Installed
systems keep working; they need the new public key before they accept
packages signed with it (`pacman-key --add` and `--lsign-key`).

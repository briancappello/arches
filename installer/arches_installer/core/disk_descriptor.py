"""Human-language disk descriptors.

Arches lets users identify physical disks using natural-language
strings rather than ``/dev/...`` paths, which can race across boots.
A descriptor like:

    device = "2TB WD SN850x SSD"

parses into a set of match predicates over the rich :class:`BlockDevice`
attributes (size, vendor, transport, rotational, model, by-id link,
serial). The :func:`match_disks` function evaluates the predicates
against a list of candidate :class:`BlockDevice` objects and returns
the ones that match every predicate.

Design rules (set during the design discussion):

1. **Size matching is tolerant** — "2TB" matches any disk between
   1.8 TB and 2.2 TB (±10%). Vendor sizes drift; users shouldn't
   need to know the exact bytecount on their device.

2. **Every token must match something.** If the descriptor contains
   ``"SN850x"`` and no candidate has that string in its model, vendor,
   serial, or by-id link, every candidate is rejected. This catches
   typos and stale specs at install time instead of after a destructive
   operation.

3. **A single role descriptor must resolve unambiguously** for
   single-disk roles. For RAID-style roles, multiple candidates with
   the same model are explicitly supported. The :func:`match_disks`
   function returns *all* candidates that match; the caller (the
   role resolver) is responsible for validating the count.

4. **A descriptor can also be a structured dict** for power users:
   ``device = {transport = "nvme", size_min = "1.8T", model_pattern = "*SN850x*"}``
   The string form is parsed into the same internal :class:`DiskCriteria`
   shape as the structured form.

This module has zero dependencies on the rest of the installer beyond
:class:`BlockDevice`, so it's easy to unit-test in isolation.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from typing import Any

from arches_installer.core.disk import BlockDevice

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

# Transports recognised in descriptors. Keys are tokens the user types;
# values are the canonical lsblk TRAN string we compare against.
_TRANSPORT_ALIASES: dict[str, str] = {
    "nvme": "nvme",
    "sata": "sata",
    "sas": "sas",
    "usb": "usb",
    "virtio": "virtio",
    # SCSI is the umbrella transport that includes SATA/SAS on most
    # kernels; treat plain "scsi" as an alias for sata for matching.
    "scsi": "sata",
}

# Storage technology tokens. "ssd" and "hdd" set the rotational flag;
# "nvme" *also* implies SSD (NVMe is always solid-state in practice,
# even if some controllers misreport the kernel rotational bit).
_TECHNOLOGY_TOKENS: dict[str, dict[str, Any]] = {
    "ssd": {"rotational": False},
    "hdd": {"rotational": True},
    "spinning": {"rotational": True},
}

# Vendor aliases — the LHS is what users type (lowercase), the RHS is a
# list of substrings any of which counts as a match against the disk's
# vendor field OR model field (NVMe vendors usually appear in MODEL,
# not the separate VENDOR column).
_VENDOR_ALIASES: dict[str, list[str]] = {
    "wd": ["wdc", "western digital"],
    "wdc": ["wdc", "western digital"],
    "western": ["wdc", "western digital"],
    "samsung": ["samsung"],
    "sammy": ["samsung"],
    "seagate": ["seagate"],
    "crucial": ["crucial", "micron"],  # Crucial is a Micron brand
    "kingston": ["kingston"],
    "sandisk": ["sandisk"],
    "intel": ["intel"],
    "micron": ["micron"],
    "toshiba": ["toshiba", "kioxia"],
    "kioxia": ["kioxia", "toshiba"],
    "hgst": ["hgst", "hitachi"],
    "hitachi": ["hitachi", "hgst"],
    "sabrent": ["sabrent"],
    "corsair": ["corsair"],
    "adata": ["adata"],
    "gigabyte": ["gigabyte"],
    "phison": ["phison"],
    "silicon": ["silicon power", "silicon motion"],
    "patriot": ["patriot"],
    "hp": ["hp"],
    "lenovo": ["lenovo"],
    "dell": ["dell"],
    "qemu": ["qemu", "virtio"],
}

# Size regex: number (with optional decimal) followed by an optional unit.
# Accepts "2TB", "2 TB", "2.5T", "512G", "1.5 TiB", "750gb".
_SIZE_RE = re.compile(
    r"^(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>[kmgtp])?i?b?$",
    re.IGNORECASE,
)

# Tolerance for size matching: a disk's bytes must fall within
# [target * (1 - TOL), target * (1 + TOL)] for a positive match.
_SIZE_TOLERANCE = 0.10  # ±10%


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class DiskCriteria:
    """A set of match predicates derived from a descriptor.

    All fields are independent AND-combined predicates. A candidate
    :class:`BlockDevice` matches only if every set predicate accepts it.

    Empty / None fields are "don't care" and impose no constraint.
    """

    # Size: stored as a center value in bytes plus the tolerance window.
    # When set, candidate.size_bytes must fall in [size_min, size_max].
    size_min: int | None = None
    size_max: int | None = None
    # Transport: canonical lsblk TRAN string, e.g. "nvme", "sata".
    transport: str | None = None
    # Rotational: True means HDD only, False means SSD only, None = either.
    rotational: bool | None = None
    # Removable flag: explicitly require removable / non-removable.
    # Auto-install workflows want non-removable; descriptors typically
    # leave this unset (callers default it).
    removable: bool | None = None
    # Vendor: a list of substrings; ANY match against either the
    # candidate's vendor field or its model field is sufficient.
    vendor_substrings: list[str] = field(default_factory=list)
    # Free-form tokens that must each match somewhere in the candidate's
    # (model + vendor + serial + by-id) attribute set. fnmatch globs are
    # supported but most tokens will be plain substrings.
    free_tokens: list[str] = field(default_factory=list)
    # Explicit device path, e.g. "/dev/nvme0n1". When set, this is a
    # short-circuit match — no other predicates are evaluated.
    explicit_device: str | None = None
    # Original descriptor string, kept for error messages.
    raw: str = ""

    @property
    def is_empty(self) -> bool:
        """True if no predicates were set (e.g. empty descriptor)."""
        return (
            self.size_min is None
            and self.transport is None
            and self.rotational is None
            and self.removable is None
            and not self.vendor_substrings
            and not self.free_tokens
            and self.explicit_device is None
        )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


class DescriptorError(ValueError):
    """Raised when a descriptor string can't be parsed coherently."""


def _parse_size_bytes(token: str) -> int | None:
    """Parse a size token into bytes. Returns None if not a size.

    Accepts decimal (k/M/G/T/P) suffixes and binary (Ki/Mi/Gi/Ti/Pi)
    suffixes; both map to the same byte counts at this granularity
    because the ±10% tolerance window absorbs the difference.

    Examples:
        "2TB"   -> 2_000_000_000_000
        "2T"    -> 2_000_000_000_000
        "512G"  -> 512_000_000_000
        "1.5T"  -> 1_500_000_000_000
        "750"   -> None  (no unit -> not a size)
    """
    m = _SIZE_RE.match(token)
    if not m:
        return None
    num_s = m.group("num")
    unit = (m.group("unit") or "").lower()
    if not unit:
        # Bare number with no unit is not a size. Could be a serial.
        return None
    try:
        num = float(num_s)
    except ValueError:
        return None
    multipliers = {
        "k": 1_000,
        "m": 1_000_000,
        "g": 1_000_000_000,
        "t": 1_000_000_000_000,
        "p": 1_000_000_000_000_000,
    }
    return int(num * multipliers[unit])


def _size_window(bytes_center: int) -> tuple[int, int]:
    """Return (min, max) bytes for the ±10% match window."""
    delta = bytes_center * _SIZE_TOLERANCE
    return (int(bytes_center - delta), int(bytes_center + delta))


def parse_descriptor(descriptor: str | dict[str, Any]) -> DiskCriteria:
    """Parse a descriptor (string or dict) into :class:`DiskCriteria`.

    String form: see module docstring. Tokens are whitespace-separated
    and case-insensitive. Tokens recognised as size/transport/tech/vendor
    are categorised; everything else is a "free token" that must match
    somewhere in the candidate's attributes.

    Dict form: maps directly to DiskCriteria fields with two
    conveniences:
        size (str)        -> size_min / size_max via tolerance
        size_min (str)    -> exact lower bound (no tolerance)
        size_max (str)    -> exact upper bound (no tolerance)
        model_pattern     -> appended as a free token (fnmatch glob)
        vendor (str)      -> appended to vendor_substrings (alias-resolved)
        device (str)      -> sets explicit_device

    Raises :class:`DescriptorError` if a structured value is malformed.
    """
    if isinstance(descriptor, dict):
        return _parse_dict_descriptor(descriptor)
    return _parse_string_descriptor(str(descriptor))


def _parse_string_descriptor(s: str) -> DiskCriteria:
    raw = s.strip()
    c = DiskCriteria(raw=raw)

    if not raw:
        return c

    # Explicit /dev path is the short-circuit escape hatch.
    if raw.startswith("/dev/"):
        c.explicit_device = raw
        return c

    # Tokenise on whitespace. We preserve original case in free_tokens
    # so the user's input shows up correctly in error messages, but
    # comparison is always case-insensitive at match time.
    tokens = raw.split()
    free_tokens: list[str] = []

    for tok in tokens:
        lower = tok.lower()

        # 1. Size?
        size = _parse_size_bytes(lower)
        if size is not None:
            if c.size_min is None and c.size_max is None:
                c.size_min, c.size_max = _size_window(size)
            else:
                # A second size token is unusual but valid — narrows
                # the window to the intersection of both.
                lo2, hi2 = _size_window(size)
                c.size_min = max(c.size_min or 0, lo2)
                c.size_max = min(c.size_max or 10**18, hi2)
            continue

        # 2. Transport?
        if lower in _TRANSPORT_ALIASES:
            c.transport = _TRANSPORT_ALIASES[lower]
            # NVMe implies SSD; let the technology setter handle that
            # so a later "HDD" token doesn't get silently overridden.
            if lower == "nvme" and c.rotational is None:
                c.rotational = False
            continue

        # 3. Storage technology (SSD / HDD)?
        if lower in _TECHNOLOGY_TOKENS:
            for k, v in _TECHNOLOGY_TOKENS[lower].items():
                # Conflict detection: writing both ssd and hdd in the
                # same descriptor is a user error.
                existing = getattr(c, k)
                if existing is not None and existing != v:
                    raise DescriptorError(
                        f"contradictory rotational flag in {raw!r}: "
                        f"already {existing}, then saw {lower!r}"
                    )
                setattr(c, k, v)
            continue

        # 4. Vendor alias?
        if lower in _VENDOR_ALIASES:
            c.vendor_substrings.extend(_VENDOR_ALIASES[lower])
            continue

        # 5. Free token (model substring / serial / by-id fragment).
        free_tokens.append(tok)

    c.free_tokens = free_tokens
    return c


def _parse_dict_descriptor(d: dict[str, Any]) -> DiskCriteria:
    c = DiskCriteria(raw=repr(d))

    # Tolerant size
    if "size" in d:
        bytes_ = _parse_size_bytes(str(d["size"]).strip().lower())
        if bytes_ is None:
            raise DescriptorError(
                f"could not parse size {d['size']!r}; use a form like '2TB'"
            )
        c.size_min, c.size_max = _size_window(bytes_)

    # Exact bounds (no tolerance)
    if "size_min" in d:
        bytes_ = _parse_size_bytes(str(d["size_min"]).strip().lower())
        if bytes_ is None:
            raise DescriptorError(f"could not parse size_min {d['size_min']!r}")
        c.size_min = bytes_
    if "size_max" in d:
        bytes_ = _parse_size_bytes(str(d["size_max"]).strip().lower())
        if bytes_ is None:
            raise DescriptorError(f"could not parse size_max {d['size_max']!r}")
        c.size_max = bytes_

    if "transport" in d:
        t = str(d["transport"]).strip().lower()
        c.transport = _TRANSPORT_ALIASES.get(t, t)

    if "rotational" in d:
        c.rotational = bool(d["rotational"])

    if "removable" in d:
        c.removable = bool(d["removable"])

    if "vendor" in d:
        v = str(d["vendor"]).strip().lower()
        if v in _VENDOR_ALIASES:
            c.vendor_substrings.extend(_VENDOR_ALIASES[v])
        else:
            c.vendor_substrings.append(v)

    if "model_pattern" in d:
        c.free_tokens.append(str(d["model_pattern"]).strip())

    if "device" in d:
        path = str(d["device"]).strip()
        if path.startswith("/dev/"):
            c.explicit_device = path
        else:
            # If someone puts a non-path string under "device" in dict
            # form, treat it as free-tokens for compatibility.
            sub = _parse_string_descriptor(path)
            return sub

    return c


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def _normalise(s: str) -> str:
    return s.lower().strip()


def _candidate_search_corpus(d: BlockDevice) -> str:
    """Concatenate every text attribute of the candidate for free-token search.

    Lower-cased, whitespace-collapsed. Includes model, vendor, serial,
    wwn, and every by-id link.
    """
    parts: list[str] = [
        d.model,
        d.vendor,
        d.serial,
        d.wwn,
        d.path,
        d.name,
        *d.by_id_links,
    ]
    return " ".join(_normalise(p) for p in parts if p)


def _vendor_corpus(d: BlockDevice) -> str:
    """Where vendor tokens can match.

    Vendor lives in different places depending on the bus:
      - NVMe: encoded in the model string ("Samsung SSD 990 PRO 2TB")
      - SATA: vendor field is often just "ATA" (the bus), with the
        real vendor in the by-id symlink ("ata-Seagate_ST8000VN004...")
      - USB: vendor field is reliable
    Search all of vendor + model + by-id links so descriptors like
    "Seagate SATA" work on real hardware.
    """
    return (
        f"{_normalise(d.vendor)} "
        f"{_normalise(d.model)} "
        f"{' '.join(_normalise(link) for link in d.by_id_links)}"
    )


def _matches_free_token(token: str, corpus: str) -> bool:
    """A free token matches if it's a substring or fnmatch glob in corpus."""
    lower = _normalise(token)
    if any(ch in lower for ch in "*?["):
        return fnmatch.fnmatchcase(corpus, f"*{lower}*") or fnmatch.fnmatchcase(
            corpus, lower
        )
    return lower in corpus


def matches(criteria: DiskCriteria, candidate: BlockDevice) -> bool:
    """True if every predicate in *criteria* accepts *candidate*."""
    # 0. Explicit device path is the strongest predicate.
    if criteria.explicit_device is not None:
        return candidate.path == criteria.explicit_device

    if criteria.is_empty:
        # No predicates -> accept anything. Defensive; callers typically
        # avoid empty criteria, but the implicit single-disk default uses
        # this path.
        return True

    # 1. Size window
    if criteria.size_min is not None or criteria.size_max is not None:
        if candidate.size_bytes <= 0:
            return False
        if criteria.size_min is not None and candidate.size_bytes < criteria.size_min:
            return False
        if criteria.size_max is not None and candidate.size_bytes > criteria.size_max:
            return False

    # 2. Transport
    if criteria.transport is not None:
        if _normalise(candidate.transport) != criteria.transport:
            return False

    # 3. Rotational
    if criteria.rotational is not None:
        # is_ssd() handles the NVMe-misreports-rotational case for us
        # when the descriptor says "ssd"; flip the logic accordingly.
        if criteria.rotational is False:
            if not candidate.is_ssd:
                return False
        else:  # rotational required
            if candidate.is_ssd:
                return False

    # 4. Removable
    if criteria.removable is not None:
        if bool(candidate.removable) != criteria.removable:
            return False

    # 5. Vendor: ANY of the listed substrings must match somewhere in
    # vendor OR model.
    if criteria.vendor_substrings:
        corpus = _vendor_corpus(candidate)
        if not any(sub in corpus for sub in criteria.vendor_substrings):
            return False

    # 6. Free tokens: EVERY token must match somewhere in the corpus.
    if criteria.free_tokens:
        corpus = _candidate_search_corpus(candidate)
        for tok in criteria.free_tokens:
            if not _matches_free_token(tok, corpus):
                return False

    return True


def match_disks(
    criteria: DiskCriteria,
    candidates: list[BlockDevice],
    *,
    exclude_removable_by_default: bool = True,
) -> list[BlockDevice]:
    """Return the subset of *candidates* matching *criteria*.

    When *exclude_removable_by_default* is True (the default), removable
    disks are filtered out unless the criteria explicitly set
    ``removable``. This matches the auto-install assumption that USB
    install media should never be a target. Set False when you want
    to consider a USB stick as a real target (e.g. portable installs).

    Results are sorted by ``(serial, path)`` so multiple matching disks
    enumerate in a stable order across boots — important for RAID role
    assignment where the (rare) absence of one disk should give a
    predictable winner among the remaining matches.
    """
    out: list[BlockDevice] = []
    for c in candidates:
        if exclude_removable_by_default and criteria.removable is None and c.removable:
            continue
        if matches(criteria, c):
            out.append(c)
    out.sort(key=lambda d: (d.serial or "", d.path))
    return out


def describe_failure(
    criteria: DiskCriteria,
    candidates: list[BlockDevice],
) -> str:
    """Build a human-friendly explanation of why a descriptor matched nothing.

    For each candidate, show which predicate rejected it. Used in
    install-time error messages so the operator can fix the descriptor
    or hardware before any destructive operation.
    """
    if criteria.explicit_device is not None:
        return (
            f"descriptor pinned to explicit device {criteria.explicit_device!r} "
            f"but no detected disk has that path"
        )

    if not candidates:
        return "no block devices detected at all"

    lines = [f"descriptor {criteria.raw!r} matched no disks. Candidates:"]
    for c in candidates:
        reasons: list[str] = []
        if criteria.size_min is not None and c.size_bytes < criteria.size_min:
            reasons.append(
                f"size {c.size} is below required min {_human(criteria.size_min)}"
            )
        if criteria.size_max is not None and c.size_bytes > criteria.size_max:
            reasons.append(
                f"size {c.size} is above required max {_human(criteria.size_max)}"
            )
        if (
            criteria.transport is not None
            and _normalise(c.transport) != criteria.transport
        ):
            reasons.append(
                f"transport is {c.transport!r}, want {criteria.transport!r}"
            )
        if criteria.rotational is True and c.is_ssd:
            reasons.append("is SSD but descriptor requires HDD")
        if criteria.rotational is False and not c.is_ssd:
            reasons.append("is HDD but descriptor requires SSD")
        if criteria.vendor_substrings:
            vc = _vendor_corpus(c)
            if not any(sub in vc for sub in criteria.vendor_substrings):
                reasons.append(
                    f"vendor/model {c.vendor!r}/{c.model!r} contains none of "
                    f"{criteria.vendor_substrings!r}"
                )
        if criteria.free_tokens:
            corpus = _candidate_search_corpus(c)
            missing = [
                tok for tok in criteria.free_tokens if not _matches_free_token(tok, corpus)
            ]
            if missing:
                reasons.append(f"missing tokens {missing!r}")
        if not reasons:
            reasons = ["(matched all predicates — internal inconsistency)"]
        lines.append(f"  - {c.path} ({c.size} {c.model!r}): {'; '.join(reasons)}")
    return "\n".join(lines)


def _human(n: int) -> str:
    """Tiny size formatter for error messages."""
    for unit, factor in [
        ("T", 10**12),
        ("G", 10**9),
        ("M", 10**6),
        ("K", 10**3),
    ]:
        if n >= factor:
            return f"{n / factor:.1f}{unit}"
    return f"{n}B"

"""Tests for the human-language disk descriptor parser and matcher."""

from __future__ import annotations

import pytest

from arches_installer.core.disk import BlockDevice
from arches_installer.core.disk_descriptor import (
    DescriptorError,
    DiskCriteria,
    _parse_size_bytes,
    _size_window,
    describe_failure,
    match_disks,
    matches,
    parse_descriptor,
)


# ---------------------------------------------------------------------------
# Test fixtures: realistic BlockDevice samples
# ---------------------------------------------------------------------------


def _samsung_990_pro_2tb(serial: str = "S5GXNX0R123456") -> BlockDevice:
    """A 2 TB Samsung 990 PRO NVMe SSD."""
    return BlockDevice(
        name="nvme0n1",
        path="/dev/nvme0n1",
        size="2T",
        size_bytes=2_000_000_000_000,
        model="Samsung SSD 990 PRO 2TB",
        vendor="",  # NVMe encodes vendor in model
        serial=serial,
        wwn="eui.0025385a01b08f55",
        transport="nvme",
        rotational=False,
        removable=False,
        partitions=[],
        by_id_links=[
            f"nvme-Samsung_SSD_990_PRO_2TB_{serial}",
            "nvme-eui.0025385a01b08f55",
        ],
    )


def _wd_sn850x_2tb() -> BlockDevice:
    return BlockDevice(
        name="nvme1n1",
        path="/dev/nvme1n1",
        size="2T",
        size_bytes=2_000_000_000_000,
        model="WDC WDS200T2X0E-00BCA0 (SN850x)",
        vendor="",
        serial="WD12345678",
        wwn="eui.e8238fa6bf530001",
        transport="nvme",
        rotational=False,
        removable=False,
        partitions=[],
        by_id_links=[
            "nvme-WDC_WDS200T2X0E-00BCA0_WD12345678",
        ],
    )


def _seagate_8tb_sata_hdd() -> BlockDevice:
    return BlockDevice(
        name="sda",
        path="/dev/sda",
        size="8T",
        size_bytes=8_000_000_000_000,
        model="ST8000VN004-3CP101",
        vendor="ATA",
        serial="WCT6L9R8",
        wwn="0x5000c500e7d8f3e9",
        transport="sata",
        rotational=True,
        removable=False,
        partitions=[],
        by_id_links=[
            "ata-Seagate_ST8000VN004-3CP101_WCT6L9R8",
            "wwn-0x5000c500e7d8f3e9",
        ],
    )


def _crucial_500gb_sata_ssd() -> BlockDevice:
    return BlockDevice(
        name="sdb",
        path="/dev/sdb",
        size="500G",
        size_bytes=500_000_000_000,
        model="CT500MX500SSD1",
        vendor="Crucial",
        serial="2147E4F12345",
        wwn="0x500a075122e4f123",
        transport="sata",
        rotational=False,
        removable=False,
        partitions=[],
        by_id_links=["ata-Crucial_CT500MX500SSD1_2147E4F12345"],
    )


def _usb_stick_64gb() -> BlockDevice:
    return BlockDevice(
        name="sdc",
        path="/dev/sdc",
        size="64G",
        size_bytes=64_000_000_000,
        model="USB 3.0 Flash Drive",
        vendor="Generic",
        serial="123456789ABC",
        wwn="",
        transport="usb",
        rotational=False,
        removable=True,
        partitions=[],
        by_id_links=["usb-Generic_USB_3.0_Flash_Drive_123456789ABC-0:0"],
    )


# ---------------------------------------------------------------------------
# _parse_size_bytes
# ---------------------------------------------------------------------------


class TestParseSizeBytes:
    def test_terabytes(self) -> None:
        assert _parse_size_bytes("2tb") == 2_000_000_000_000
        assert _parse_size_bytes("2t") == 2_000_000_000_000

    def test_decimal(self) -> None:
        assert _parse_size_bytes("1.5t") == 1_500_000_000_000

    def test_gigabytes(self) -> None:
        assert _parse_size_bytes("512g") == 512_000_000_000
        assert _parse_size_bytes("512gb") == 512_000_000_000

    def test_megabytes(self) -> None:
        assert _parse_size_bytes("100m") == 100_000_000

    def test_petabytes(self) -> None:
        assert _parse_size_bytes("1p") == 1_000_000_000_000_000

    def test_kibibytes_treated_as_kilobytes(self) -> None:
        # Tolerance window absorbs the difference; we don't distinguish
        # decimal vs binary at the regex level.
        assert _parse_size_bytes("2tib") == 2_000_000_000_000

    def test_no_unit_returns_none(self) -> None:
        assert _parse_size_bytes("2000") is None

    def test_invalid_returns_none(self) -> None:
        assert _parse_size_bytes("foo") is None
        assert _parse_size_bytes("") is None
        assert _parse_size_bytes("twoT") is None


class TestSizeWindow:
    def test_ten_percent_tolerance(self) -> None:
        lo, hi = _size_window(2_000_000_000_000)
        assert lo == 1_800_000_000_000
        assert hi == 2_200_000_000_000

    def test_real_disk_within_window(self) -> None:
        # Samsung 990 PRO 2TB ships at ~2.0 TB (vendor units).
        lo, hi = _size_window(2_000_000_000_000)
        assert lo <= 1_900_000_000_000 <= hi
        # Some "2TB" disks report 1.92 TiB == ~2.11 TB — also in window.
        assert lo <= 2_110_000_000_000 <= hi


# ---------------------------------------------------------------------------
# String descriptor parsing
# ---------------------------------------------------------------------------


class TestParseString:
    def test_empty(self) -> None:
        c = parse_descriptor("")
        assert c.is_empty
        assert c.raw == ""

    def test_size_only(self) -> None:
        c = parse_descriptor("2TB")
        assert c.size_min == 1_800_000_000_000
        assert c.size_max == 2_200_000_000_000
        assert c.transport is None
        assert c.free_tokens == []

    def test_transport_only(self) -> None:
        c = parse_descriptor("nvme")
        assert c.transport == "nvme"
        # NVMe implies SSD
        assert c.rotational is False

    def test_ssd_token(self) -> None:
        c = parse_descriptor("SSD")
        assert c.rotational is False
        assert c.transport is None

    def test_hdd_token(self) -> None:
        c = parse_descriptor("HDD")
        assert c.rotational is True

    def test_ssd_hdd_conflict(self) -> None:
        with pytest.raises(DescriptorError, match="contradictory"):
            parse_descriptor("SSD HDD")

    def test_vendor_alias_wd(self) -> None:
        c = parse_descriptor("WD")
        assert "wdc" in c.vendor_substrings
        assert "western digital" in c.vendor_substrings

    def test_vendor_alias_case_insensitive(self) -> None:
        c1 = parse_descriptor("samsung")
        c2 = parse_descriptor("SAMSUNG")
        c3 = parse_descriptor("Sammy")
        assert c1.vendor_substrings == c2.vendor_substrings == c3.vendor_substrings

    def test_explicit_device_short_circuit(self) -> None:
        c = parse_descriptor("/dev/nvme0n1")
        assert c.explicit_device == "/dev/nvme0n1"
        # Everything else is ignored when explicit_device set.
        assert c.free_tokens == []

    def test_compound_descriptor(self) -> None:
        c = parse_descriptor("2TB WD SN850x SSD")
        assert c.size_min == 1_800_000_000_000
        assert c.size_max == 2_200_000_000_000
        assert c.rotational is False
        assert "wdc" in c.vendor_substrings
        assert c.free_tokens == ["SN850x"]

    def test_compound_with_transport(self) -> None:
        c = parse_descriptor("8TB Seagate SATA")
        assert c.size_min == 7_200_000_000_000
        assert c.size_max == 8_800_000_000_000
        assert c.transport == "sata"
        assert "seagate" in c.vendor_substrings

    def test_free_token_preserved_case(self) -> None:
        c = parse_descriptor("SN850x")
        assert c.free_tokens == ["SN850x"]


# ---------------------------------------------------------------------------
# Dict descriptor parsing
# ---------------------------------------------------------------------------


class TestParseDict:
    def test_size_with_tolerance(self) -> None:
        c = parse_descriptor({"size": "2T"})
        assert c.size_min == 1_800_000_000_000
        assert c.size_max == 2_200_000_000_000

    def test_explicit_size_bounds(self) -> None:
        c = parse_descriptor({"size_min": "1T", "size_max": "4T"})
        assert c.size_min == 1_000_000_000_000
        assert c.size_max == 4_000_000_000_000

    def test_transport(self) -> None:
        c = parse_descriptor({"transport": "nvme"})
        assert c.transport == "nvme"

    def test_rotational_flag(self) -> None:
        c = parse_descriptor({"rotational": False})
        assert c.rotational is False

    def test_vendor_alias(self) -> None:
        c = parse_descriptor({"vendor": "wd"})
        assert "wdc" in c.vendor_substrings

    def test_vendor_unknown_passthrough(self) -> None:
        c = parse_descriptor({"vendor": "weirdco"})
        assert c.vendor_substrings == ["weirdco"]

    def test_model_pattern(self) -> None:
        c = parse_descriptor({"model_pattern": "*SN850x*"})
        assert "*SN850x*" in c.free_tokens

    def test_explicit_device(self) -> None:
        c = parse_descriptor({"device": "/dev/nvme0n1"})
        assert c.explicit_device == "/dev/nvme0n1"

    def test_device_with_descriptor_string(self) -> None:
        # device = "2TB Samsung" should parse as a string descriptor.
        c = parse_descriptor({"device": "2TB Samsung"})
        assert c.size_min is not None
        assert "samsung" in c.vendor_substrings

    def test_invalid_size_raises(self) -> None:
        with pytest.raises(DescriptorError, match="parse"):
            parse_descriptor({"size": "two terabytes"})


# ---------------------------------------------------------------------------
# matches()
# ---------------------------------------------------------------------------


class TestMatches:
    def test_size_match(self) -> None:
        c = parse_descriptor("2TB")
        assert matches(c, _samsung_990_pro_2tb())
        assert not matches(c, _seagate_8tb_sata_hdd())

    def test_size_just_outside_window(self) -> None:
        # An "2TB" descriptor matches 1.8TB to 2.2TB; a 1.5 TB disk
        # should not match.
        d = _samsung_990_pro_2tb()
        d.size_bytes = 1_500_000_000_000
        c = parse_descriptor("2TB")
        assert not matches(c, d)

    def test_transport_match(self) -> None:
        c = parse_descriptor({"transport": "nvme"})
        assert matches(c, _samsung_990_pro_2tb())
        assert not matches(c, _seagate_8tb_sata_hdd())

    def test_ssd_excludes_hdd(self) -> None:
        c = parse_descriptor("SSD")
        assert matches(c, _samsung_990_pro_2tb())
        assert not matches(c, _seagate_8tb_sata_hdd())

    def test_hdd_excludes_ssd(self) -> None:
        c = parse_descriptor("HDD")
        assert matches(c, _seagate_8tb_sata_hdd())
        assert not matches(c, _samsung_990_pro_2tb())

    def test_vendor_match_via_model(self) -> None:
        # NVMe vendor encoded in model string — vendor_substrings must
        # match the model corpus.
        c = parse_descriptor("Samsung")
        assert matches(c, _samsung_990_pro_2tb())
        assert not matches(c, _wd_sn850x_2tb())

    def test_vendor_match_via_vendor_field(self) -> None:
        # SATA devices typically have vendor field populated separately.
        c = parse_descriptor("crucial")
        assert matches(c, _crucial_500gb_sata_ssd())
        # NB: "crucial" alias also matches "micron" — Crucial is a
        # Micron brand.
        assert not matches(c, _seagate_8tb_sata_hdd())

    def test_free_token_against_serial(self) -> None:
        d = _samsung_990_pro_2tb(serial="S5GXNX0R123456")
        c = parse_descriptor("S5GXNX0R123456")
        assert matches(c, d)

    def test_free_token_against_by_id(self) -> None:
        c = parse_descriptor("SN850x")
        assert matches(c, _wd_sn850x_2tb())

    def test_free_token_glob(self) -> None:
        c = parse_descriptor({"model_pattern": "*990_PRO*"})
        assert matches(c, _samsung_990_pro_2tb())

    def test_strict_unmatched_token_rejects_all(self) -> None:
        # "DoesNotExist" is a free token that won't match anything.
        # Strict mode (default) must reject the candidate.
        c = parse_descriptor("Samsung DoesNotExist")
        assert not matches(c, _samsung_990_pro_2tb())

    def test_compound_must_satisfy_every_predicate(self) -> None:
        # "2TB Samsung" - size + vendor both required.
        c = parse_descriptor("2TB Samsung")
        assert matches(c, _samsung_990_pro_2tb())
        # Same model in a 1.5 TB form should fail size predicate.
        d = _samsung_990_pro_2tb()
        d.size_bytes = 1_500_000_000_000
        assert not matches(c, d)

    def test_explicit_device_short_circuit(self) -> None:
        c = parse_descriptor("/dev/nvme0n1")
        assert matches(c, _samsung_990_pro_2tb())
        # Different device path → no match even with same attributes.
        d = _samsung_990_pro_2tb()
        d.path = "/dev/nvme9n9"
        assert not matches(c, d)

    def test_empty_criteria_accepts_any(self) -> None:
        c = DiskCriteria()
        assert matches(c, _samsung_990_pro_2tb())
        assert matches(c, _seagate_8tb_sata_hdd())


# ---------------------------------------------------------------------------
# match_disks() — filtering + ordering
# ---------------------------------------------------------------------------


class TestMatchDisks:
    def test_returns_only_matching(self) -> None:
        candidates = [
            _samsung_990_pro_2tb(),
            _seagate_8tb_sata_hdd(),
            _crucial_500gb_sata_ssd(),
        ]
        c = parse_descriptor("SSD")
        out = match_disks(c, candidates)
        paths = [d.path for d in out]
        assert "/dev/nvme0n1" in paths
        assert "/dev/sdb" in paths
        assert "/dev/sda" not in paths

    def test_excludes_removable_by_default(self) -> None:
        candidates = [_samsung_990_pro_2tb(), _usb_stick_64gb()]
        c = parse_descriptor("SSD")
        out = match_disks(c, candidates)
        # USB stick has rotational=False but is removable — excluded.
        assert all(not d.removable for d in out)

    def test_includes_removable_when_descriptor_requests(self) -> None:
        candidates = [_usb_stick_64gb()]
        c = parse_descriptor({"removable": True, "transport": "usb"})
        out = match_disks(c, candidates)
        assert len(out) == 1

    def test_raid_two_matching(self) -> None:
        """Two identical Samsung 990 PROs should both match — RAID case."""
        a = _samsung_990_pro_2tb(serial="S5GXNX0R000001")
        b = _samsung_990_pro_2tb(serial="S5GXNX0R000002")
        b.path = "/dev/nvme1n1"
        b.name = "nvme1n1"
        c = parse_descriptor("2TB Samsung 990")
        out = match_disks(c, [a, b])
        assert len(out) == 2

    def test_stable_ordering_by_serial(self) -> None:
        """match_disks sorts by serial so RAID role assignment is stable."""
        a = _samsung_990_pro_2tb(serial="S5GXNX0R_ZZZ")
        b = _samsung_990_pro_2tb(serial="S5GXNX0R_AAA")
        b.path = "/dev/nvme1n1"
        b.name = "nvme1n1"
        c = parse_descriptor("2TB Samsung")
        out = match_disks(c, [a, b])
        # Sorted by serial: AAA before ZZZ.
        assert out[0].serial == "S5GXNX0R_AAA"
        assert out[1].serial == "S5GXNX0R_ZZZ"


# ---------------------------------------------------------------------------
# describe_failure()
# ---------------------------------------------------------------------------


class TestDescribeFailure:
    def test_lists_per_candidate_reasons(self) -> None:
        c = parse_descriptor("2TB Samsung SSD")
        candidates = [_seagate_8tb_sata_hdd(), _crucial_500gb_sata_ssd()]
        msg = describe_failure(c, candidates)
        # Seagate fails on size AND vendor AND rotational
        assert "above required max" in msg
        # Crucial fails on size AND vendor
        assert "below required min" in msg
        # Both fail on vendor (no Samsung)
        assert "samsung" in msg.lower()

    def test_no_candidates_message(self) -> None:
        c = parse_descriptor("2TB")
        msg = describe_failure(c, [])
        assert "no block devices" in msg

    def test_explicit_device_message(self) -> None:
        c = parse_descriptor("/dev/nvme0n1")
        msg = describe_failure(c, [_seagate_8tb_sata_hdd()])
        assert "/dev/nvme0n1" in msg
        assert "no detected disk has that path" in msg


# ---------------------------------------------------------------------------
# Real-world descriptors from the design discussion
# ---------------------------------------------------------------------------


class TestRealWorldDescriptors:
    """End-to-end tests mirroring the examples from design docs."""

    def test_2tb_wd_sn850x_ssd(self) -> None:
        c = parse_descriptor("2TB WD SN850x SSD")
        assert matches(c, _wd_sn850x_2tb())
        assert not matches(c, _samsung_990_pro_2tb())  # wrong vendor

    def test_8tb_seagate_sata(self) -> None:
        c = parse_descriptor("8TB Seagate SATA")
        assert matches(c, _seagate_8tb_sata_hdd())
        assert not matches(c, _samsung_990_pro_2tb())  # wrong transport

    def test_1tb_nvme_implicit_ssd(self) -> None:
        # 1TB NVMe — rotational should be implicitly False because NVMe
        # implies SSD.
        d = _samsung_990_pro_2tb()
        d.size_bytes = 1_000_000_000_000
        c = parse_descriptor("1TB NVMe")
        assert matches(c, d)
        # Same descriptor should reject an HDD even though HDDs cannot
        # actually be NVMe — guarding the predicate logic.
        d.transport = "sata"
        d.rotational = True
        assert not matches(c, d)

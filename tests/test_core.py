"""Offline checks for radio preservation and CHIRP image handling.

The fixture is synthetic: no customer's image, serial port or radio is used.
Its reserved bits and unused memory contain deliberate non-default values.
"""

import base64
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from radio_lote import core


def bcd_frequency(frequency_hz):
    """The BF-888 stores eight decimal digits, little-endian, in 10 Hz units."""
    return bytes.fromhex(f"{frequency_hz // 10:08d}")[::-1]


def synthetic_raw():
    data = bytearray((index * 37 + 11) & 0xFF for index in range(0x3E0))
    for channel in range(1, 17):
        start = 0x10 + (channel - 1) * 16
        frequency = 462_125_000 + (channel - 1) * 100_000
        data[start : start + 4] = bcd_frequency(frequency)
        data[start + 4 : start + 8] = bcd_frequency(frequency)
        data[start + 8 : start + 12] = b"\xff" * 4
        # Reserved bits set, and independently varied scan/power/narrow/BCL bits.
        data[start + 12] = 0xE0 | ((channel * 7) & 0x1F)
    return bytes(data)


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.raw = synthetic_raw()
        self.profile = core.profile_from_raw(self.raw)

    def test_profile_reads_first_four_channels_and_uses_new_dcs(self):
        self.assertEqual(self.profile["schema_version"], 1)
        self.assertEqual(self.profile["client"], "futebol")
        self.assertEqual(self.profile["driver"], "Baofeng_BF-888")
        self.assertEqual(self.profile["chirp_revision"], core.CHIRP_REVISION)
        channels = self.profile["channels"]
        self.assertEqual([item["channel"] for item in channels], [1, 2, 3, 4])
        self.assertEqual([item["dcs"] for item in channels], [743, 731, 712, 703])
        self.assertEqual([item["polarity"] for item in channels], ["NN"] * 4)
        for number, item in enumerate(channels):
            self.assertEqual(item["rx_hz"], 462_125_000 + number * 100_000)
            self.assertEqual(item["tx_hz"], item["rx_hz"])
        core.validate_profile(self.profile)

    def test_rejects_bad_profile_values_before_building(self):
        mutations = {
            "duplicate_frequency": lambda p: p["channels"][1].update(
                rx_hz=p["channels"][0]["rx_hz"], tx_hz=p["channels"][0]["tx_hz"]
            ),
            "repeater_offset": lambda p: p["channels"][0].update(
                tx_hz=p["channels"][0]["rx_hz"] + 5_000_000
            ),
            "out_of_band": lambda p: p["channels"][0].update(
                rx_hz=145_000_000, tx_hz=145_000_000
            ),
            "not_representable": lambda p: p["channels"][0].update(
                rx_hz=462_125_001, tx_hz=462_125_001
            ),
            "missing_channel": lambda p: p["channels"].pop(),
            "duplicate_channel": lambda p: p["channels"][1].update(channel=1),
            "unsupported_dcs": lambda p: p["channels"][0].update(dcs=999),
            "unsupported_polarity": lambda p: p["channels"][0].update(polarity="ZZ"),
            "unsupported_mode": lambda p: p["channels"][0].update(mode="AM"),
            "wrong_driver": lambda p: p.update(driver="Baofeng_UV-5R"),
            "wrong_revision": lambda p: p.update(chirp_revision="another-version"),
        }
        for name, change in mutations.items():
            with self.subTest(name=name):
                profile = copy.deepcopy(self.profile)
                change(profile)
                with self.assertRaises(core.RadioLoteError):
                    core.validate_profile(profile)
                with self.assertRaises(core.RadioLoteError):
                    core.build_candidate(self.raw, profile)

    def test_rejects_empty_initial_channel(self):
        data = bytearray(self.raw)
        data[0x10:0x20] = b"\xff" * 16
        with self.assertRaises(core.RadioLoteError):
            core.profile_from_raw(bytes(data))

    def test_rejects_initial_radio_with_duplicate_frequencies(self):
        data = bytearray(self.raw)
        data[0x20:0x28] = data[0x10:0x18]
        with self.assertRaises(core.RadioLoteError):
            core.profile_from_raw(bytes(data))


class PreservationTests(unittest.TestCase):
    def setUp(self):
        self.raw = synthetic_raw()
        self.profile = core.profile_from_raw(self.raw)

    def test_updates_profile_fields_without_normalizing_unrelated_bytes(self):
        profile = copy.deepcopy(self.profile)
        for channel in profile["channels"]:
            channel["rx_hz"] += 2_000_000
            channel["tx_hz"] += 2_000_000
            channel["mode"] = "FM" if channel["mode"] == "NFM" else "NFM"

        candidate = core.build_candidate(self.raw, profile)
        self.assertEqual(len(candidate), 0x3E0)
        self.assertEqual(self.raw, synthetic_raw(), "Input image must remain untouched")
        self.assertNotEqual(candidate, self.raw)
        allowed = set()
        for channel in range(1, 5):
            start = 0x10 + (channel - 1) * 16
            allowed.update(range(start, start + 13))
            self.assertEqual(candidate[start + 12] & ~0x04, self.raw[start + 12] & ~0x04)
            self.assertEqual(candidate[start + 13 : start + 16], self.raw[start + 13 : start + 16])

        changed = {i for i, (old, new) in enumerate(zip(self.raw, candidate)) if old != new}
        self.assertTrue(changed <= allowed)
        self.assertEqual(candidate[:0x10], self.raw[:0x10])
        self.assertEqual(candidate[0x50:], self.raw[0x50:])
        self.assertEqual(core.summarize(candidate)[4:], core.summarize(self.raw)[4:])

        for channel in profile["channels"]:
            start = 0x10 + (channel["channel"] - 1) * 16
            encoded = bcd_frequency(channel["rx_hz"])
            self.assertEqual(candidate[start : start + 4], encoded)
            self.assertEqual(candidate[start + 4 : start + 8], encoded)
            self.assertEqual(bool(candidate[start + 12] & 0x04), channel["mode"] == "NFM")

    def test_writes_same_dcs_on_tx_rx_with_normal_polarity(self):
        candidate = core.build_candidate(self.raw, self.profile)
        expected_tones = [b"\x43\x87", b"\x31\x87", b"\x12\x87", b"\x03\x87"]
        summary = core.summarize(candidate)
        for index, expected in enumerate(expected_tones):
            start = 0x10 + index * 16
            self.assertEqual(candidate[start + 8 : start + 10], expected)
            self.assertEqual(candidate[start + 10 : start + 12], expected)
            self.assertEqual(summary[index]["tone_mode"], "DTCS")
            self.assertEqual(summary[index]["dcs"], self.profile["channels"][index]["dcs"])
            self.assertEqual(summary[index]["rx_dcs"], self.profile["channels"][index]["dcs"])
            self.assertEqual(summary[index]["polarity"], "NN")

    def test_same_profile_is_idempotent(self):
        candidate = core.build_candidate(self.raw, self.profile)
        self.assertEqual(core.build_candidate(candidate, self.profile), candidate)

    def test_rejects_truncated_and_oversized_memory(self):
        for invalid in (self.raw[:-1], self.raw + b"\0"):
            with self.subTest(length=len(invalid)):
                with self.assertRaises(core.RadioLoteError):
                    core.build_candidate(invalid, self.profile)

    def test_readback_detects_changes_even_outside_the_four_channels(self):
        core.verify_raw(self.raw, bytes(self.raw))
        for address in (0x18, 0x55, 0x2B9, 0x3DF):
            with self.subTest(address=hex(address)):
                changed = bytearray(self.raw)
                changed[address] ^= 1
                with self.assertRaises(core.RadioLoteError):
                    core.verify_raw(bytes(changed), self.raw)
        with self.assertRaises(core.RadioLoteError):
            core.verify_raw(self.raw[:-1], self.raw)


class ExtendedProfileTests(unittest.TestCase):
    def setUp(self):
        self.raw = synthetic_raw()

    def test_normalization_is_a_deep_copy_and_preserves_legacy_polarity(self):
        legacy = core.profile_from_raw(self.raw)
        legacy["channels"][0]["polarity"] = "RN"
        normalized = core.normalize_profile(legacy)
        self.assertEqual(normalized["schema_version"], 2)
        self.assertEqual(normalized["channels"][0]["tone_mode"], "DTCS")
        self.assertEqual(normalized["channels"][0]["polarity"], "RN")
        normalized["channels"][0]["dcs"] = 23
        self.assertEqual(legacy["channels"][0]["dcs"], 743)
        self.assertNotIn("tone_mode", legacy["channels"][0])

    def test_new_group_codes_are_generated_once_and_reused_for_all_devices(self):
        codes = list(core.chirp_common.DTCS_CODES[:16])
        with mock.patch.object(core.secrets.SystemRandom, "sample", return_value=codes) as sample:
            profile = core.make_profile(self.raw, count=16)
            first = core.build_candidate(self.raw, profile)
            second = core.build_candidate(self.raw, profile)
        sample.assert_called_once()
        self.assertEqual(first, second)
        self.assertEqual(len(set(item["dcs"] for item in profile["channels"])), 16)
        self.assertEqual([item["channel"] for item in profile["channels"]], list(range(1, 17)))

    def test_integration_allows_repeated_frequencies_but_new_groups_do_not(self):
        raw = bytearray(self.raw)
        raw[0x20:0x28] = raw[0x10:0x18]
        integrated = core.make_profile(bytes(raw), count=2, new_groups=False)
        self.assertEqual(integrated["channels"][0]["rx_hz"], integrated["channels"][1]["rx_hz"])
        self.assertEqual(core.build_candidate(bytes(raw), integrated), bytes(raw))
        with self.assertRaises(core.RadioLoteError):
            core.make_profile(bytes(raw), count=2, new_groups=True)

    def test_sparse_positions_none_ctcss_and_reverse_dcs_preserve_other_bytes(self):
        all_channels = core.make_profile(self.raw, count=16, new_groups=False)
        profile = copy.deepcopy(all_channels)
        profile["channels"] = [profile["channels"][index] for index in (0, 6, 15)]
        profile["channels"][0].update(tone_mode="", ctcss=None, label="Sem filtro")
        profile["channels"][1].update(tone_mode="TSQL", ctcss=88.5, label="CTCSS")
        profile["channels"][2].update(tone_mode="DTCS", dcs=743, polarity="NR", ctcss=None)
        for channel in profile["channels"]:
            channel["rx_hz"] += 2_000_000
            channel["tx_hz"] += 2_000_000
            channel["mode"] = "NFM" if channel["mode"] == "FM" else "FM"
        candidate = core.build_candidate(self.raw, profile)
        allowed = set()
        for number in (1, 7, 16):
            start = 0x10 + 16 * (number - 1)
            allowed.update(range(start, start + 13))
            self.assertEqual(candidate[start + 12] & ~4, self.raw[start + 12] & ~4)
        changed = {index for index, (old, new) in enumerate(zip(self.raw, candidate)) if old != new}
        self.assertTrue(changed <= allowed)
        self.assertEqual(candidate[0x18:0x1C], b"\xff\xff\xff\xff")
        self.assertEqual(candidate[0x78:0x7C], b"\x85\x08\x85\x08")
        self.assertEqual(candidate[0x108:0x10C], b"\x43\xc7\x43\x87")
        self.assertEqual(core.build_candidate(candidate, profile), candidate)
        integrated = core.make_profile(candidate, count=16, new_groups=False)
        self.assertEqual(integrated["channels"][6]["ctcss"], 88.5)
        self.assertEqual(integrated["channels"][15]["polarity"], "NR")

    def test_integration_rejects_asymmetric_and_unrepresentable_tones(self):
        for rx, tx in ((b"\xff\xff", b"\x85\x08"),
                       (b"\x23\x80", b"\x25\x80"),
                       (b"\x00\x11", b"\x00\x11")):
            with self.subTest(rx=rx, tx=tx):
                raw = bytearray(self.raw)
                raw[0x18:0x1A], raw[0x1A:0x1C] = rx, tx
                with self.assertRaises(core.RadioLoteError):
                    core.make_profile(bytes(raw), count=1, new_groups=False)

    def test_schema2_rejects_unsupported_fields_and_tone_combinations(self):
        base = core.make_profile(self.raw, count=1, new_groups=False)
        mutations = (
            lambda p: p["channels"][0].update(channel=17),
            lambda p: p["channels"][0].update(tone_mode="Cross"),
            lambda p: p["channels"][0].update(tone_mode="TSQL", ctcss=66.6),
            lambda p: p["channels"][0].update(tone_mode="TSQL", ctcss=None),
            lambda p: p["channels"][0].update(tone_mode="DTCS", ctcss=88.5),
            lambda p: p["channels"][0].update(label="bad\nlabel"),
            lambda p: p["channels"][0].update(power="High"),
            lambda p: p.update(channels=[]),
        )
        for mutation in mutations:
            invalid = copy.deepcopy(base)
            mutation(invalid)
            with self.assertRaises(core.RadioLoteError):
                core.validate_profile(invalid)
        for count in (0, 17, True, 1.5):
            with self.assertRaises(core.RadioLoteError):
                core.make_profile(self.raw, count=count)


class ImageTests(unittest.TestCase):
    def test_image_roundtrip_preserves_all_raw_bytes(self):
        raw = synthetic_raw()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "backup.img"
            core.save_image(path, raw)
            self.assertEqual(core.load_image(path), raw)
            self.assertGreater(path.stat().st_size, len(raw), "Saved image must include model metadata")

    def test_existing_backup_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "backup.img"
            core.save_image(path, synthetic_raw())
            original_file = path.read_bytes()
            replacement = bytearray(synthetic_raw())
            replacement[0x2B9] ^= 1
            with self.assertRaises((core.RadioLoteError, FileExistsError)):
                core.save_image(path, bytes(replacement))
            self.assertEqual(path.read_bytes(), original_file)

    def test_rejects_raw_without_model_metadata_and_truncated_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "invalid.img"
            for data in (synthetic_raw(), synthetic_raw()[:100]):
                with self.subTest(length=len(data)):
                    path.write_bytes(data)
                    with self.assertRaises(core.RadioLoteError):
                        core.load_image(path)

    def test_rejects_image_identifying_another_radio_family(self):
        magic = b"\x00\xffchirp\xeeimg\x00\x01"
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "backup.img"
            core.save_image(path, synthetic_raw())
            original = path.read_bytes()
            prefix = 0x3E0 + len(magic)
            metadata = json.loads(base64.b64decode(original[prefix:]))
            for field, value in (("vendor", "Other"), ("model", "UV-5R"),
                                 ("rclass", "UV5RRadio"), ("variant", "unsupported")):
                with self.subTest(field=field):
                    changed = dict(metadata, **{field: value})
                    path.write_bytes(original[:prefix] + base64.b64encode(json.dumps(changed).encode()))
                    with self.assertRaises(core.RadioLoteError):
                        core.load_image(path)

    def test_rejects_corrupt_model_metadata(self):
        magic = b"\x00\xffchirp\xeeimg\x00\x01"
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "corrupt.img"
            for suffix in (b"invalid!", base64.b64encode(b"{"), base64.b64encode(b"[]")):
                with self.subTest(suffix=suffix):
                    path.write_bytes(synthetic_raw() + magic + suffix)
                    with self.assertRaises(core.RadioLoteError):
                        core.load_image(path)


class PortSelectionTests(unittest.TestCase):
    def test_single_usb_port_is_selected_without_opening_it(self):
        ports = [{"device": "/dev/cu.usbserial-test", "description": "Test USB", "hwid": "USB TEST"}]
        with mock.patch.object(core, "list_usb_ports", return_value=ports):
            self.assertEqual(core.resolve_port(), ports[0]["device"])

    def test_no_port_and_ambiguous_port_are_errors(self):
        first = {"device": "/dev/cu.usbserial-one", "description": "One", "hwid": "USB ONE"}
        second = {"device": "/dev/cu.usbserial-two", "description": "Two", "hwid": "USB TWO"}
        for ports in ([], [first, second]):
            with self.subTest(count=len(ports)), mock.patch.object(core, "list_usb_ports", return_value=ports):
                with self.assertRaises(core.RadioLoteError):
                    core.resolve_port()

    def test_explicit_selection_disambiguates_two_cables(self):
        ports = [
            {"device": "/dev/cu.usbserial-one", "description": "One", "hwid": "USB ONE"},
            {"device": "/dev/cu.usbserial-two", "description": "Two", "hwid": "USB TWO"},
        ]
        with mock.patch.object(core, "list_usb_ports", return_value=ports):
            self.assertEqual(core.resolve_port(ports[1]["device"]), ports[1]["device"])


if __name__ == "__main__":
    unittest.main()

"""Unit tests for the bitfield decoders in charger_app.

These are the bits of the codebase that translate raw 16-bit register
values into human strings.  They have no I/O dependencies, so they're
the easiest part to test thoroughly — and they're exactly the kind of
code where a typo (off-by-one bit, wrong label) silently destroys an
hour of debugging when you read the wrong bit from a register.

Run with:  pytest tests/
"""
import pytest

from charger_app import (
    CHG_STATUS_BITS,
    FAULT_BITS,
    SYSTEM_STATUS_BITS,
    _decode_bits,
    _decode_curve_config,
    _decode_system_config,
)


# ---------------------------------------------------------------------------
# _decode_bits — the generic worker
# ---------------------------------------------------------------------------


class TestDecodeBits:
    def test_zero_returns_empty_list(self):
        assert _decode_bits(0, FAULT_BITS) == []
        assert _decode_bits(0, CHG_STATUS_BITS) == []
        assert _decode_bits(0, SYSTEM_STATUS_BITS) == []

    def test_single_bit_set(self):
        # FAULT_BITS bit 2 = OVP
        assert _decode_bits(1 << 2, FAULT_BITS) == ["OVP (over-voltage)"]

    def test_multiple_bits_set_in_table_order(self):
        # Bits 1 (OTP), 4 (SHORT), 7 (HI_TEMP) — table is sorted by bit index.
        value = (1 << 1) | (1 << 4) | (1 << 7)
        assert _decode_bits(value, FAULT_BITS) == [
            "OTP (over-temperature)",
            "SHORT (short-circuit)",
            "HI_TEMP (internal high temp)",
        ]

    def test_bit_not_in_table_is_ignored(self):
        # FAULT_BITS has no entry for bit 0 or bit 8+; setting them has no effect.
        assert _decode_bits(1 << 0,  FAULT_BITS) == []
        assert _decode_bits(1 << 8,  FAULT_BITS) == []
        assert _decode_bits(0xFFFF, FAULT_BITS) == [
            "OTP (over-temperature)",
            "OVP (over-voltage)",
            "OLP (over-load)",
            "SHORT (short-circuit)",
            "AC_FAIL (AC abnormal)",
            "HI_TEMP (internal high temp)",
        ]

    def test_chg_status_full_word(self):
        # Real-world: CCM (bit 1) + HI_TEMP (bit 7).
        value = (1 << 1) | (1 << 7)
        assert _decode_bits(value, CHG_STATUS_BITS) == [
            "CCM (in CC mode)",
            "HI_TEMP (internal high temp)",
        ]

    def test_chg_status_timeout_flags(self):
        # The *TOF bits live in the high byte (13/14/15).
        value = (1 << 13) | (1 << 14) | (1 << 15)
        assert _decode_bits(value, CHG_STATUS_BITS) == [
            "CCTOF (CC mode timed out)",
            "CVTOF (CV mode timed out)",
            "FVTOF (float mode timed out)",
        ]

    def test_system_status_dc_ok_default(self):
        # Demo bus reports SYSTEM_STATUS = 0x0002 (DC_OK only).
        assert _decode_bits(0x0002, SYSTEM_STATUS_BITS) == [
            "DC_OK (DC output normal)",
        ]


# ---------------------------------------------------------------------------
# _decode_curve_config — the most error-prone register
# ---------------------------------------------------------------------------
#
# Bit layout per the manual and the corrections documented in README
# "Notes from the field":
#   bits 0-1   CUVS    curve select (0=custom, 1-3=preset)
#   bits 2-3   TCS     temp comp slope (0=off, 1=-3, 2=-4, 3=-5 mV/C/cell)
#   bit  5     CVTSSE  CV→float transition (0=cut-off, 1=enter float)
#   bit  7     CUVE    mode (0=PSU, 1=charger)
#   bit  8     CVTOE   CV timeout enable
#   bit  9     CCTOE   CC timeout enable
#   bit 10     FVTOE   FV timeout enable
#   bit 11     RSTE    restart-on-Vbat enable


class TestDecodeCurveConfig:
    def test_all_zero(self):
        out = _decode_curve_config(0x0000)
        assert "curve=customised" in out
        assert "temp_comp=off"    in out
        assert "cv_timeout_action=cut-off" in out
        assert "mode=PSU"         in out
        assert "cv_timeout_en=off" in out
        assert "cc_timeout_en=off" in out
        assert "fv_timeout_en=off" in out
        assert "restart_en=off"    in out

    def test_factory_default_0x0084(self):
        # 0x0084 = 0000 0000 1000 0100 = CUVE (bit 7) + TCS=01 (bits 2-3)
        #        = charger mode + -3 mV/C/cell temp comp
        out = _decode_curve_config(0x0084)
        assert "curve=customised"      in out
        assert "temp_comp=-3mV/C/cell" in out
        assert "mode=charger"          in out
        assert "restart_en=off"        in out

    def test_recommended_0x0884(self):
        # 0x0884 = factory default + RSTE (bit 11) — the README's
        # "Suggested 16S LFP values" recommendation.
        out = _decode_curve_config(0x0884)
        assert "mode=charger"          in out
        assert "temp_comp=-3mV/C/cell" in out
        assert "restart_en=on"         in out

    def test_curve_select_bits(self):
        # CUVS = bits 0-1
        assert "curve=customised" in _decode_curve_config(0b00)
        assert "curve=preset 1"   in _decode_curve_config(0b01)
        assert "curve=preset 2"   in _decode_curve_config(0b10)
        assert "curve=preset 3"   in _decode_curve_config(0b11)

    @pytest.mark.parametrize("tcs_bits,label", [
        (0b00 << 2, "temp_comp=off"),
        (0b01 << 2, "temp_comp=-3mV/C/cell"),
        (0b10 << 2, "temp_comp=-4mV/C/cell"),
        (0b11 << 2, "temp_comp=-5mV/C/cell"),
    ])
    def test_temp_comp_slope(self, tcs_bits, label):
        # Note: README "Manual chapter 6 has real typos" documents that
        # the page-52 prose lists 01/01/01 for TCS values; the correct
        # decoding is 00/01/10/11 -> off / -3 / -4 / -5.
        assert label in _decode_curve_config(tcs_bits)

    def test_cvtsse_bit5(self):
        # CVTSSE = bit 5: 0 = cut-off when CV completes, 1 = enter float
        assert "cv_timeout_action=cut-off"     in _decode_curve_config(0x0000)
        assert "cv_timeout_action=enter float" in _decode_curve_config(1 << 5)

    def test_charger_vs_psu_mode_bit7(self):
        assert "mode=PSU"     in _decode_curve_config(0x0000)
        assert "mode=charger" in _decode_curve_config(1 << 7)

    @pytest.mark.parametrize("bit,label_on,label_off", [
        (8,  "cv_timeout_en=on", "cv_timeout_en=off"),   # CVTOE
        (9,  "cc_timeout_en=on", "cc_timeout_en=off"),   # CCTOE
        (10, "fv_timeout_en=on", "fv_timeout_en=off"),   # FVTOE
        (11, "restart_en=on",    "restart_en=off"),       # RSTE
    ])
    def test_high_byte_enable_bits(self, bit, label_on, label_off):
        # The README "Notes from the field" calls out this whole
        # CVTOE/CCTOE/FVTOE/RSTE band as the bits most often miscoded
        # against the manual's prose — the bit numbering below comes
        # straight from the bit-position diagram, which is correct.
        assert label_off in _decode_curve_config(0)
        assert label_on  in _decode_curve_config(1 << bit)

    def test_all_high_byte_bits_set(self):
        # 0x0F00 = CVTOE | CCTOE | FVTOE | RSTE all on
        out = _decode_curve_config(0x0F00)
        for label in ("cv_timeout_en=on", "cc_timeout_en=on",
                      "fv_timeout_en=on", "restart_en=on"):
            assert label in out


# ---------------------------------------------------------------------------
# _decode_system_config
# ---------------------------------------------------------------------------
#
# Layout per the manual:
#   bits 1-2   OP_INIT     power-on state (0=OFF, 1=ON, 2=last, 3=reserved)
#   bit  10    EEP_OFF     EEPROM-write disable


class TestDecodeSystemConfig:
    @pytest.mark.parametrize("raw,expected_state", [
        (0b00 << 1, "power_on_state=OFF"),
        (0b01 << 1, "power_on_state=ON"),
        (0b10 << 1, "power_on_state=last setting"),
        (0b11 << 1, "power_on_state=reserved"),
    ])
    def test_power_on_state(self, raw, expected_state):
        assert expected_state in _decode_system_config(raw)

    def test_eeprom_writes_enabled_by_default(self):
        # Bit 10 = 0 means writes are enabled (the safe / normal state).
        assert "eeprom_writes=enabled" in _decode_system_config(0x0000)

    def test_eeprom_writes_disabled_when_bit10_set(self):
        assert "eeprom_writes=disabled" in _decode_system_config(1 << 10)

    def test_combined_power_on_eeprom(self):
        # Demo bus default: 0x0002 = OP_INIT=01 (ON), EEP_OFF=0 (enabled).
        out = _decode_system_config(0x0002)
        assert "power_on_state=ON" in out
        assert "eeprom_writes=enabled" in out


# ---------------------------------------------------------------------------
# Cross-cutting sanity: the bit tables themselves
# ---------------------------------------------------------------------------


class TestBitTables:
    @pytest.mark.parametrize("table", [
        FAULT_BITS, CHG_STATUS_BITS, SYSTEM_STATUS_BITS,
    ])
    def test_bit_indices_are_unique(self, table):
        # If two entries map to the same bit, _decode_bits would emit
        # both whenever that bit is set — surprising and almost certainly
        # a typo.
        indices = [bit for bit, _name, _desc in table]
        assert len(indices) == len(set(indices)), \
            f"duplicate bit indices in {indices}"

    @pytest.mark.parametrize("table", [
        FAULT_BITS, CHG_STATUS_BITS, SYSTEM_STATUS_BITS,
    ])
    def test_bit_indices_in_range(self, table):
        # 16-bit registers, so bits should be in [0, 15].
        for bit, _name, _desc in table:
            assert 0 <= bit <= 15, f"bit {bit} out of range"

    @pytest.mark.parametrize("table", [
        FAULT_BITS, CHG_STATUS_BITS, SYSTEM_STATUS_BITS,
    ])
    def test_names_are_unique(self, table):
        names = [name for _bit, name, _desc in table]
        assert len(names) == len(set(names)), \
            f"duplicate bit names in {names}"

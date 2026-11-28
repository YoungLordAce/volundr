"""Automated end-to-end unit & integration tests for Phase 3 (`converter.py` + `router.py`)."""

from __future__ import annotations

import ctypes
from dataclasses import replace
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

from src.config_loader import load_config
from src.models import PostProcessAction
from src.router import ingest_target, parse_multidisc_info
from src.watcher import scan_watch_dir_once
from tests.test_phase2 import _build_iso9660_image

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.example.toml"


class Phase3Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.base_config = load_config(CONFIG_PATH)

    def _make_isolated_config(
        self,
        root: Path,
        post_action: PostProcessAction = PostProcessAction.MOVE,
    ):
        drop = root / "drop"
        staging = root / ".staging"
        processed = root / "processed"
        quarantine = root / "quarantine"
        nas = root / "mock_nas_roms"
        for d in (drop, staging, processed, quarantine, nas):
            d.mkdir(parents=True, exist_ok=True)

        new_paths = replace(
            self.base_config.paths,
            watch_dir=drop,
            staging_dir=staging,
            processed_dir=processed,
            quarantine_dir=quarantine,
            library_root=nas,
            raw_library_root=str(nas),
        )
        new_general = replace(
            self.base_config.general,
            post_process_action=post_action,
            min_free_staging_gb=0.01,
        )
        return replace(self.base_config, paths=new_paths, general=new_general)

    def test_multidisc_parser(self) -> None:
        self.assertEqual(
            parse_multidisc_info("Final Fantasy VII (USA) (Disc 1)"),
            ("Final Fantasy VII (USA)", 1),
        )
        self.assertEqual(
            parse_multidisc_info("Metal Gear Solid (Europe) (Disc 2) (Rev 1)"),
            ("Metal Gear Solid (Europe) (Rev 1)", 2),
        )
        self.assertIsNone(parse_multidisc_info("Kingdom Hearts Final Mix"))

    def test_psx_async_multidisc_7z_to_chd_and_posix_m3u(self) -> None:
        seven_zip = self.base_config.binaries.seven_zip
        chdman = self.base_config.binaries.chdman
        if not seven_zip or not chdman:
            self.skipTest("Requires 7z and chdman")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base_cfg = self._make_isolated_config(root, PostProcessAction.MOVE)
            cfg = replace(
                base_cfg,
                conversion=replace(base_cfg.conversion, psx_multidisc_pbp=False),
            )
            build_tmp = root / "build"
            build_tmp.mkdir()

            # Build Disc 1 and Disc 2 .7z archives
            for disc_num in (1, 2):
                stem = f"Final Fantasy VII (USA) (Disc {disc_num})"
                bin_f = build_tmp / f"{stem}.bin"
                cue_f = build_tmp / f"{stem}.cue"
                bin_f.write_bytes(
                    _build_iso9660_image(
                        sector_size=2352,
                        data_offset=24,
                        sys_ident="PLAYSTATION",
                        files={"SYSTEM.CNF;1": f"BOOT = cdrom:\\SCUS_941.6{disc_num};1\r\n".encode()},
                        total_sectors=48,
                    )
                )
                cue_f.write_text(
                    f'FILE "{stem}.bin" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n',
                    encoding="utf-8",
                )
                arc_7z = cfg.paths.watch_dir / f"{stem}.7z"
                subprocess.run(
                    [str(seven_zip), "a", "-y", str(arc_7z), str(bin_f), str(cue_f)],
                    check=True,
                    capture_output=True,
                )

            # 1. Dry-run check on Disc 1 first (must leave everything untouched)
            arc1 = cfg.paths.watch_dir / "Final Fantasy VII (USA) (Disc 1).7z"
            arc2 = cfg.paths.watch_dir / "Final Fantasy VII (USA) (Disc 2).7z"
            dry = ingest_target(arc1, cfg, dry_run=True)
            self.assertTrue(dry.dry_run)
            self.assertTrue(arc1.exists())
            self.assertFalse((cfg.paths.library_root / "psx").exists())

            # 2. Ingest Disc 1 live -> verify .discs/ placement + .m3u + processed move + empty staging
            out1 = ingest_target(arc1, cfg)
            self.assertFalse(arc1.exists())
            self.assertTrue((cfg.paths.processed_dir / arc1.name).is_file())
            self.assertEqual(list(cfg.paths.staging_dir.iterdir()), [])

            psx_dir = cfg.paths.library_root / "psx"
            disc1_chd = psx_dir / "_discs" / "Final Fantasy VII (USA) (Disc 1).chd"
            m3u_file = psx_dir / "Final Fantasy VII (USA).m3u"
            self.assertTrue(disc1_chd.is_file())
            self.assertGreater(disc1_chd.stat().st_size, 0)
            if sys.platform == "win32":
                attrs = ctypes.windll.kernel32.GetFileAttributesW(str(disc1_chd))
                self.assertEqual(attrs & 0x02, 0, "Routed file must NOT have FILE_ATTRIBUTE_HIDDEN set")
            self.assertTrue(m3u_file.is_file())
            self.assertEqual(
                m3u_file.read_text(encoding="utf-8").splitlines(),
                ["_discs/Final Fantasy VII (USA) (Disc 1).chd"],
            )

            # 3. Ingest Disc 2 minutes later -> .m3u idempotently updates with POSIX `/` paths!
            out2 = ingest_target(arc2, cfg)
            self.assertEqual(len(out2.m3u_playlists), 1)
            m3u_lines = m3u_file.read_text(encoding="utf-8").splitlines()
            self.assertEqual(
                m3u_lines,
                [
                    "_discs/Final Fantasy VII (USA) (Disc 1).chd",
                    "_discs/Final Fantasy VII (USA) (Disc 2).chd",
                ],
            )
            # Ensure zero `~syncthing~*.tmp` or `.syncthing.*.tmp` temp files left behind
            syncthing_temps = [
                p for p in psx_dir.rglob("*")
                if p.name.startswith(("~syncthing~", ".syncthing."))
            ]
            self.assertEqual(syncthing_temps, [])

    def test_psp_2048_hunk_chd_and_arcade_passthrough(self) -> None:
        chdman = self.base_config.binaries.chdman
        if not chdman:
            self.skipTest("Requires chdman")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._make_isolated_config(root, PostProcessAction.DELETE)

            # 1. PSP .iso -> createdvd -hs 2048 -> .chd
            psp_iso = cfg.paths.watch_dir / "Lumines.iso"
            psp_iso.write_bytes(
                _build_iso9660_image(
                    sector_size=2048,
                    data_offset=0,
                    sys_ident="PSP GAME",
                    files={"UMD_DATA.BIN;1": b"ULUS-10002|0001"},
                    dirs=("PSP_GAME",),
                    total_sectors=64,
                )
            )
            res_psp = ingest_target(psp_iso, cfg)
            self.assertFalse(psp_iso.exists())  # Deleted per PostProcessAction.DELETE
            psp_chd = cfg.paths.library_root / "psp" / "Lumines.chd"
            self.assertTrue(psp_chd.is_file())
            # Verify CHD v5 hunkbytes header at 0x38..0x3C == 2048!
            hdr = psp_chd.read_bytes()[:64]
            self.assertEqual(hdr[:8], b"MComprHD")
            self.assertEqual(int.from_bytes(hdr[56:60], "big"), 2048)

            # 2. Arcade CPS3 zip -> passthrough without extraction
            cps3_zip = cfg.paths.watch_dir / "sfiii3.zip"
            with zipfile.ZipFile(cps3_zip, "w") as zf:
                zf.writestr("10", b"\xaa" * 512)
                zf.writestr("20", b"\xbb" * 512)
            ingest_target(cps3_zip, cfg)
            self.assertTrue((cfg.paths.library_root / "cps3" / "sfiii3.zip").is_file())

    def test_3ds_cci_normalization_cia_subdir_and_quarantine_safety(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Even when configured to DELETE, quarantined items must override to MOVE!
            cfg = self._make_isolated_config(root, PostProcessAction.DELETE)

            # 1. Decrypted .3ds -> renamed to .cci in 3ds/
            dec_3ds = cfg.paths.watch_dir / "MarioKart7.3ds"
            buf_dec = bytearray(0x4200)
            buf_dec[0x100:0x104] = b"NCSD"
            buf_dec[0x120:0x124] = (0x20).to_bytes(4, "little")
            buf_dec[0x4100:0x4104] = b"NCCH"
            buf_dec[0x418F] = 0x04
            dec_3ds.write_bytes(buf_dec)

            ingest_target(dec_3ds, cfg)
            self.assertTrue((cfg.paths.library_root / "3ds" / "MarioKart7.cci").is_file())
            self.assertFalse(dec_3ds.exists())

            # 2. Installable .cia -> 3ds/_cia_install/
            cia = cfg.paths.watch_dir / "MarioKart7_Update_v1.2.cia"
            cia.write_bytes(b"CIA_PACKAGE_PAYLOAD" * 64)
            ingest_target(cia, cfg)
            self.assertTrue(
                (cfg.paths.library_root / "3ds" / "_cia_install" / cia.name).is_file()
            )

            # 3. Encrypted .3ds (without ctr_keys) -> quarantined + source preserved in processed/ despite DELETE mode!
            cfg_no_keys = replace(
                cfg,
                config_path=root / "config.toml",
                binaries=replace(cfg.binaries, ctr_keys=None),
            )
            enc_3ds = cfg_no_keys.paths.watch_dir / "Encrypted3DS.3ds"
            buf_enc = bytearray(buf_dec)
            buf_enc[0x418F] = 0x00
            enc_3ds.write_bytes(buf_enc)
            out_q = ingest_target(enc_3ds, cfg_no_keys)
            self.assertEqual(len(out_q.quarantined_files), 1)
            self.assertTrue((cfg_no_keys.paths.quarantine_dir / "Encrypted3DS.3ds").is_file())
            self.assertTrue((cfg_no_keys.paths.processed_dir / "Encrypted3DS.3ds").is_file())

    def test_3ds_encrypted_rom_auto_decryption_with_aes_keys(self) -> None:
        try:
            from Crypto.Cipher import AES  # type: ignore[import-not-found]
        except ImportError:
            try:
                from Cryptodome.Cipher import AES  # type: ignore[import-not-found]
            except ImportError:
                self.skipTest("Requires pycryptodome")

        from src.converter import ConversionError
        from src.ctr_decrypt import scramble_3ds_key
        from src.identifier import _check_3ds_encryption

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._make_isolated_config(root, PostProcessAction.MOVE)

            # Synthetic 3DS keyslots (non-copyrighted test keys)
            keyx_2c = bytes.fromhex("00112233445566778899AABBCCDDEEFF")
            keyx_25 = bytes.fromhex("FFEEDDCCBBAA99887766554433221100")
            key_y = bytes.fromhex("0123456789ABCDEF0123456789ABCDEF")
            keys_file = root / "aes_keys.txt"
            keys_file.write_text(
                f"generator=1FF9E9AAC5FE0408024591DC5D52768A\n"
                f"slot0x2CKeyX={keyx_2c.hex()}\n"
                f"slot0x25KeyX={keyx_25.hex()}\n",
                encoding="utf-8",
            )
            cfg_keys = replace(cfg, binaries=replace(cfg.binaries, ctr_keys=keys_file))

            primary_k = scramble_3ds_key(keyx_2c, key_y)
            sec_k = scramble_3ds_key(keyx_25, key_y)

            # Build synthetic 7.x NCSD+NCCH image:
            # 0x4000: NCCH header (0x200)
            # 0x4200: ExHeader (0x800)
            # 0x4A00: ExeFS (0x600: 0x200 header + 0x200 icon + 0x200 .code)
            # 0x5000: RomFS (0x400 starting with IVFC)
            part_off = 0x4000
            part_len = 0x1400
            img = bytearray(part_off + part_len)
            img[0x100:0x104] = b"NCSD"
            img[0x120:0x124] = (part_off // 0x200).to_bytes(4, "little")
            img[0x124:0x128] = (part_len // 0x200).to_bytes(4, "little")

            pid_bytes = bytes.fromhex("0001020304050607")
            pid_be = pid_bytes[::-1]
            hdr = bytearray(0x200)
            hdr[0x000:0x010] = key_y
            hdr[0x100:0x104] = b"NCCH"
            hdr[0x104:0x108] = (part_len // 0x200).to_bytes(4, "little")
            hdr[0x108:0x110] = pid_bytes
            hdr[0x112:0x114] = (2).to_bytes(2, "little")  # NCCH v2
            hdr[0x180:0x184] = (0x400).to_bytes(4, "little")  # ExHeader present
            hdr[0x18B] = 0x01  # 7.x secondary crypto (slot 0x25)
            hdr[0x18D] = 0x03  # Retail 4 KiB content alignment flag (must NOT multiply 0x200 media units!)
            hdr[0x18F] = 0x00  # Encrypted (NoCrypto NOT set)
            hdr[0x1A0:0x1A4] = (0xA00 // 0x200).to_bytes(4, "little")  # ExeFS off
            hdr[0x1A4:0x1A8] = (0x600 // 0x200).to_bytes(4, "little")  # ExeFS len
            hdr[0x1B0:0x1B4] = (0x1000 // 0x200).to_bytes(4, "little")  # RomFS off
            hdr[0x1B4:0x1B8] = (0x400 // 0x200).to_bytes(4, "little")  # RomFS len
            img[part_off : part_off + 0x200] = hdr

            plain_exh = b"EXHDR_PLAINTEXT_" * (0x800 // 16)
            exefs_hdr = bytearray(0x200)
            exefs_hdr[0:8] = b"icon\x00\x00\x00\x00"
            exefs_hdr[8:12] = (0).to_bytes(4, "little")
            exefs_hdr[12:16] = (0x200).to_bytes(4, "little")
            exefs_hdr[16:24] = b".code\x00\x00\x00"
            exefs_hdr[24:28] = (0x200).to_bytes(4, "little")
            exefs_hdr[28:32] = (0x200).to_bytes(4, "little")
            plain_icon = b"ICON_DATA_BLOCK_" * (0x200 // 16)
            plain_code = b"CODE_DATA_BLOCK_" * (0x200 // 16)
            plain_romfs = (b"IVFC\x00\x00\x01\x00" + (b"ROMFS_PAYLOAD_OK" * 64))[:0x400]

            # Encrypt each section with its exact NCCH v2 key & counter
            iv_exh = pid_be + b"\x01" + (b"\x00" * 7)
            img[part_off + 0x200 : part_off + 0xA00] = AES.new(
                primary_k, AES.MODE_CTR, nonce=b"", initial_value=iv_exh
            ).encrypt(plain_exh)

            exefs_iv_int = int.from_bytes(pid_be + b"\x02" + (b"\x00" * 7), "big")
            img[part_off + 0xA00 : part_off + 0xC00] = AES.new(
                primary_k, AES.MODE_CTR, nonce=b"", initial_value=exefs_iv_int.to_bytes(16, "big")
            ).encrypt(bytes(exefs_hdr))
            img[part_off + 0xC00 : part_off + 0xE00] = AES.new(
                primary_k, AES.MODE_CTR, nonce=b"", initial_value=(exefs_iv_int + (0x200 // 16)).to_bytes(16, "big")
            ).encrypt(plain_icon)
            img[part_off + 0xE00 : part_off + 0x1000] = AES.new(
                sec_k, AES.MODE_CTR, nonce=b"", initial_value=(exefs_iv_int + (0x400 // 16)).to_bytes(16, "big")
            ).encrypt(plain_code)

            iv_romfs = pid_be + b"\x03" + (b"\x00" * 7)
            img[part_off + 0x1000 : part_off + 0x1400] = AES.new(
                sec_k, AES.MODE_CTR, nonce=b"", initial_value=iv_romfs
            ).encrypt(plain_romfs)

            enc_rom = cfg_keys.paths.watch_dir / "Fire Emblem Awakening (axekin.com).3ds"
            enc_rom.write_bytes(img)

            out = ingest_target(enc_rom, cfg_keys)
            self.assertEqual(out.quarantined_files, ())
            out_cci = cfg_keys.paths.library_root / "3ds" / "Fire Emblem Awakening.cci"
            self.assertTrue(out_cci.is_file())
            is_enc_after, _ = _check_3ds_encryption(out_cci)
            self.assertFalse(is_enc_after)

            dec = out_cci.read_bytes()
            self.assertEqual(dec[part_off + 0x18B], 0x00)
            self.assertTrue(bool(dec[part_off + 0x18F] & 0x04))
            self.assertEqual(dec[part_off + 0x200 : part_off + 0xA00], plain_exh)
            self.assertEqual(dec[part_off + 0xA00 : part_off + 0xC00], bytes(exefs_hdr))
            self.assertEqual(dec[part_off + 0xC00 : part_off + 0xE00], plain_icon)
            self.assertEqual(dec[part_off + 0xE00 : part_off + 0x1000], plain_code)
            self.assertEqual(dec[part_off + 0x1000 : part_off + 0x1400], plain_romfs)

            # Verify bad keys fail loudly via IVFC check without routing garbage
            bad_keys = root / "bad_aes_keys.txt"
            bad_keys.write_text("slot0x2CKeyX=00000000000000000000000000000000\nslot0x25KeyX=00000000000000000000000000000000\n", encoding="utf-8")
            cfg_bad = replace(cfg, binaries=replace(cfg.binaries, ctr_keys=bad_keys))
            bad_rom = cfg_bad.paths.watch_dir / "BadKeysTest.3ds"
            bad_rom.write_bytes(img)
            with self.assertRaises(ConversionError) as ctx:
                ingest_target(bad_rom, cfg_bad)
            self.assertIn("IVFC", str(ctx.exception))

    def test_3ds_base_game_cia_to_cci_and_update_dlc_routing(self) -> None:
        from src.ctr_decrypt import inspect_3ds_header, inspect_cia_header, load_seeddb_map

        def _make_synthetic_cia(title_id: int, ncch_payload: bytes) -> bytes:
            header_size = 0x2020
            cert_size = 0x40
            tik_size = 0x40
            sig_block = (0x010004).to_bytes(4, "big") + (b"\x00" * 0x13C)  # RSA-2048 SHA256
            tmd_body = bytearray(0x9C4 + 0x30)
            tmd_body[0x4C:0x54] = title_id.to_bytes(8, "big")
            tmd_body[0x9E:0xA0] = (1).to_bytes(2, "big")
            # Chunk 0 (unencrypted CIA content)
            rec = 0x9C4
            tmd_body[rec : rec + 4] = (0).to_bytes(4, "big")       # content_id
            tmd_body[rec + 4 : rec + 6] = (0).to_bytes(2, "big")   # cindex 0
            tmd_body[rec + 6 : rec + 8] = (0).to_bytes(2, "big")   # ctype (unencrypted)
            tmd_body[rec + 8 : rec + 16] = len(ncch_payload).to_bytes(8, "big")
            tmd_bytes = sig_block + bytes(tmd_body)

            cia_hdr = bytearray(0x2040)
            cia_hdr[0x00:0x04] = header_size.to_bytes(4, "little")
            cia_hdr[0x08:0x0C] = cert_size.to_bytes(4, "little")
            cia_hdr[0x0C:0x10] = tik_size.to_bytes(4, "little")
            cia_hdr[0x10:0x14] = len(tmd_bytes).to_bytes(4, "little")
            cia_hdr[0x18:0x20] = len(ncch_payload).to_bytes(8, "little")
            tmd_pad = ((len(tmd_bytes) + 63) & ~63) - len(tmd_bytes)
            return bytes(cia_hdr) + (b"\x00" * cert_size) + (b"\x00" * tik_size) + tmd_bytes + (b"\x00" * tmd_pad) + ncch_payload

        # Build a minimal valid decrypted NCCH partition (0x400 bytes, NoCrypto set)
        ncch = bytearray(0x400)
        ncch[0x100:0x104] = b"NCCH"
        ncch[0x104:0x108] = (len(ncch) // 0x200).to_bytes(4, "little")
        ncch[0x108:0x110] = (0x0004000000123400).to_bytes(8, "little")
        ncch[0x118:0x120] = (0x0004000000123400).to_bytes(8, "little")
        ncch[0x18F] = 0x04  # NoCrypto (already decrypted)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._make_isolated_config(root, PostProcessAction.MOVE)

            # 1. Base Game `.cia` (`00040000...`) -> auto-converted to decrypted `.cci` in `3ds/`
            game_cia = cfg.paths.watch_dir / "Severed (Europe) (eShop).cia"
            game_cia.write_bytes(_make_synthetic_cia(0x0004000000123400, bytes(ncch)))
            info_g = inspect_cia_header(game_cia)
            self.assertIsNotNone(info_g)
            assert info_g is not None
            self.assertEqual(info_g.category, "game")
            self.assertTrue(info_g.is_convertible_game)

            out_game = ingest_target(game_cia, cfg)
            expected_cci = cfg.paths.library_root / "3ds" / "Severed (Europe) (eShop).cci"
            self.assertTrue(expected_cci.is_file())
            self.assertEqual(out_game.routed_files, (expected_cci,))
            is_enc, _, _ = inspect_3ds_header(expected_cci)
            self.assertFalse(is_enc)
            cci_raw = expected_cci.read_bytes()
            self.assertEqual(cci_raw[0x100:0x104], b"NCSD")
            self.assertEqual(cci_raw[0x4100:0x4104], b"NCCH")

            # 2. Update `.cia` (`0004000e...`) & DLC `.cia` (`0004008c...`) -> routed to `3ds/_cia_install/`
            upd_cia = cfg.paths.watch_dir / "Game (Update).cia"
            upd_cia.write_bytes(_make_synthetic_cia(0x0004000E00123400, bytes(ncch)))
            info_u = inspect_cia_header(upd_cia)
            self.assertIsNotNone(info_u)
            assert info_u is not None
            self.assertEqual(info_u.category, "update")
            self.assertFalse(info_u.is_convertible_game)
            ingest_target(upd_cia, cfg)
            self.assertTrue((cfg.paths.library_root / "3ds" / "_cia_install" / "Game (Update).cia").is_file())

            dlc_cia = cfg.paths.watch_dir / "Game (DLC).cia"
            dlc_cia.write_bytes(_make_synthetic_cia(0x0004008C00123400, bytes(ncch)))
            info_d = inspect_cia_header(dlc_cia)
            self.assertIsNotNone(info_d)
            assert info_d is not None
            self.assertEqual(info_d.category, "dlc")
            self.assertFalse(info_d.is_convertible_game)
            ingest_target(dlc_cia, cfg)
            self.assertTrue((cfg.paths.library_root / "3ds" / "_cia_install" / "Game (DLC).cia").is_file())

            # 3. Verify `load_seeddb_map` parses binary `seeddb.bin` entries
            seed_bytes = bytes(range(16))
            seeddb = root / "seeddb.bin"
            seeddb.write_bytes(
                (1).to_bytes(4, "little")
                + (b"\x00" * 12)
                + (0x0004000000165F00).to_bytes(8, "little")
                + seed_bytes
                + (b"\x00" * 8)
            )
            seed_map = load_seeddb_map(seeddb)
            self.assertEqual(seed_map.get(0x0004000000165F00), seed_bytes)

    def test_mangled_watermark_cue_healing_pbp_and_nas_m3u_cleanup(self) -> None:
        chdman = self.base_config.binaries.chdman
        psxpackager = self.base_config.binaries.psxpackager
        if not chdman or not psxpackager:
            self.skipTest("Requires chdman and psxpackager")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._make_isolated_config(root, PostProcessAction.MOVE)
            psx_dir = cfg.paths.library_root / "psx"

            # Seed legacy NAS `_discs/*.chd` + `.m3u` to verify automatic superseded cleanup!
            old_discs_dir = psx_dir / "_discs"
            old_discs_dir.mkdir(parents=True, exist_ok=True)
            old_d1 = old_discs_dir / "Metal Gear Solid (Europe) (Disc 1).chd"
            old_d2 = old_discs_dir / "Metal Gear Solid (Europe) (Disc 2).chd"
            old_m3u = psx_dir / "Metal Gear Solid (Europe).m3u"
            old_d1.write_bytes(b"LEGACY_CHD_1")
            old_d2.write_bytes(b"LEGACY_CHD_2")
            old_m3u.write_text(
                "_discs/Metal Gear Solid (Europe) (Disc 1).chd\n"
                "_discs/Metal Gear Solid (Europe) (Disc 2).chd\n",
                encoding="utf-8",
            )

            # 1. Exact reproduction of `Metal Gear Solid (axekin.com).zip`:
            #    - Disc 1 `.cue` filename clobbered to `axekin.com Gear Solid (Europe) (Disc 1).cue`
            #    - Disc 2 `.cue` internal `FILE` line desynced from `Metal Gear Solid (Europe) (Disc 2).bin`
            mgs_zip = cfg.paths.watch_dir / "Metal Gear Solid (axekin.com).zip"
            ps1_img_1 = _build_iso9660_image(
                sector_size=2352,
                data_offset=24,
                sys_ident="PLAYSTATION",
                files={"SYSTEM.CNF;1": b"BOOT = cdrom:\\SLES_013.70;1\r\n"},
                total_sectors=64,
            )
            ps1_img_2 = _build_iso9660_image(
                sector_size=2352,
                data_offset=24,
                sys_ident="PLAYSTATION",
                files={"SYSTEM.CNF;1": b"BOOT = cdrom:\\SLES_113.70;1\r\n"},
                total_sectors=64,
            )
            with zipfile.ZipFile(mgs_zip, "w") as zf:
                d1 = "Metal Gear Solid (axekin.com)/Metal Gear Solid (axekin.com) (Disc 1)"
                zf.writestr(f"{d1}/Metal Gear Solid (axekin.com) (Disc 1).bin", ps1_img_1)
                zf.writestr(
                    f"{d1}/axekin.com Gear Solid (Europe) (Disc 1).cue",
                    'FILE "Metal Gear Solid (axekin.com) (Disc 1).bin" BINARY\n'
                    "  TRACK 01 MODE2/2352\n"
                    "    INDEX 01 00:00:00\n",
                )
                zf.writestr(f"{d1}/Metal Gear Solid (Europe) (Disc 1).sbi", b"SBI1" * 16)
                d2 = "Metal Gear Solid (axekin.com)/Metal Gear Solid (axekin.com) (Disc 2)"
                zf.writestr(f"{d2}/Metal Gear Solid (Europe) (Disc 2).bin", ps1_img_2)
                # Intentionally desynced FILE name (caused the original `nan%` hang!)
                zf.writestr(
                    f"{d2}/Metal Gear Solid (Europe) (Disc 2).cue",
                    'FILE "Metal Gear Solid (axekin.com) (Disc 2).bin" BINARY\n'
                    "  TRACK 01 MODE2/2352\n"
                    "    INDEX 01 00:00:00\n",
                )
                zf.writestr(f"{d2}/Metal Gear Solid (Europe) (Disc 2).sbi", b"SBI2" * 16)

            # Dry-run predicts `.pbp` and superseded legacy NAS files without modifying disk
            dry_mgs = ingest_target(mgs_zip, cfg, dry_run=True)
            self.assertEqual(dry_mgs.routed_files, (psx_dir / "Metal Gear Solid (Europe).pbp",))
            self.assertEqual(dry_mgs.m3u_playlists, ())
            self.assertEqual(len(dry_mgs.superseded_files), 3)
            self.assertTrue(old_m3u.is_file())

            # Live run packs both healed discs into a single `.pbp` and cleans up legacy `.m3u` + `_discs/`
            out_mgs = ingest_target(mgs_zip, cfg)
            self.assertEqual(out_mgs.quarantined_files, ())
            self.assertEqual(out_mgs.m3u_playlists, ())

            pbp_out = psx_dir / "Metal Gear Solid (Europe).pbp"
            self.assertTrue(pbp_out.is_file())
            self.assertEqual(pbp_out.read_bytes()[:4], b"\x00PBP")
            sub_dir = psx_dir / "subchannels"
            self.assertTrue((sub_dir / "Metal Gear Solid (Europe) (Disc 1).sbi").is_file())
            self.assertTrue((sub_dir / "Metal Gear Solid (Europe) (Disc 2).sbi").is_file())
            self.assertTrue((sub_dir / "Metal Gear Solid (Europe)_1.sbi").is_file())
            self.assertTrue((sub_dir / "Metal Gear Solid (Europe).sbi").is_file())
            # Ensure no loose .sbi files remain in root psx_dir so frontends (Cocoon) don't create ghost cards
            self.assertEqual(list(psx_dir.glob("*.sbi")), [])
            self.assertFalse(old_m3u.exists())
            self.assertFalse(old_d1.exists())
            self.assertFalse(old_d2.exists())
            self.assertFalse(old_discs_dir.exists())
            self.assertEqual(len(out_mgs.superseded_files), 3)

            # 2. Single-disc cue-less raw `.bin` still converts to `.chd` (not `.pbp`)
            raw_bin = cfg.paths.watch_dir / "Silent Hill (USA) (axekin.com).bin"
            raw_bin.write_bytes(ps1_img_1)
            out_raw = ingest_target(raw_bin, cfg)
            self.assertEqual(out_raw.quarantined_files, ())
            self.assertTrue((psx_dir / "Silent Hill (USA).chd").is_file())

    def test_psx_sibling_disc_archives_and_nas_chd_donor_to_pbp(self) -> None:
        chdman = self.base_config.binaries.chdman
        psxpackager = self.base_config.binaries.psxpackager
        if not chdman or not psxpackager:
            self.skipTest("Requires chdman and psxpackager")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._make_isolated_config(root, PostProcessAction.MOVE)
            psx_dir = cfg.paths.library_root / "psx"

            def _make_disc_zip(zip_path: Path, disc_stem: str, boot_code: str) -> None:
                img = _build_iso9660_image(
                    sector_size=2352,
                    data_offset=24,
                    sys_ident="PLAYSTATION",
                    files={"SYSTEM.CNF;1": f"BOOT = cdrom:\\{boot_code};1\r\n".encode()},
                    total_sectors=64,
                )
                with zipfile.ZipFile(zip_path, "w") as zf:
                    zf.writestr(f"{disc_stem}.bin", img)
                    zf.writestr(
                        f"{disc_stem}.cue",
                        f'FILE "{disc_stem}.bin" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n',
                    )

            # A. Two separate `.zip` archives in `drop/` bundled by `scan_watch_dir_once` into 1 `.pbp`
            _make_disc_zip(
                cfg.paths.watch_dir / "Chrono Cross (USA) (Disc 1).zip",
                "Chrono Cross (USA) (Disc 1)",
                "SLUS_010.41",
            )
            _make_disc_zip(
                cfg.paths.watch_dir / "Chrono Cross (USA) (Disc 2).zip",
                "Chrono Cross (USA) (Disc 2)",
                "SLUS_010.80",
            )
            outcomes, errs = scan_watch_dir_once(cfg, dry_run=False, verify_stability=False)
            self.assertEqual(errs, 0)
            self.assertEqual(len(outcomes), 1)
            cc_pbp = psx_dir / "Chrono Cross (USA).pbp"
            self.assertTrue(cc_pbp.is_file())
            self.assertEqual(cc_pbp.read_bytes()[:4], b"\x00PBP")
            self.assertEqual(len(outcomes[0].additional_sources), 1)

            # B. Async arrival: Disc 1 arrives alone -> `.chd`; Disc 2 arrives later -> upgraded to `.pbp`!
            xeno_d1 = root / "Xenogears (USA) (Disc 1).zip"
            xeno_d2 = root / "Xenogears (USA) (Disc 2).zip"
            _make_disc_zip(xeno_d1, "Xenogears (USA) (Disc 1)", "SLUS_006.64")
            _make_disc_zip(xeno_d2, "Xenogears (USA) (Disc 2)", "SLUS_006.69")

            out_x1 = ingest_target(xeno_d1, cfg)
            xeno_d1_chd = psx_dir / "Xenogears (USA) (Disc 1).chd"
            self.assertTrue(xeno_d1_chd.is_file())
            self.assertEqual(out_x1.m3u_playlists, ())

            out_x2 = ingest_target(xeno_d2, cfg)
            xeno_pbp = psx_dir / "Xenogears (USA).pbp"
            self.assertTrue(xeno_pbp.is_file())
            self.assertEqual(xeno_pbp.read_bytes()[:4], b"\x00PBP")
            self.assertFalse(xeno_d1_chd.exists())
            self.assertIn(xeno_d1_chd, out_x2.superseded_files)

    def test_plain_text_inspect_reports_psx_multidisc_pbp_merge_and_iso9660_serial(self) -> None:
        import io
        from unittest.mock import patch
        import main as cli_main
        from src.converter import extract_psx_disc_serial

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._make_isolated_config(root)

            # 1. Verify extract_psx_disc_serial reads ISO9660 SYSTEM.CNF via SectorReader
            psx_iso = root / "Disc1.iso"
            psx_iso.write_bytes(
                _build_iso9660_image(
                    sector_size=2352,
                    data_offset=24,
                    sys_ident="PLAYSTATION",
                    files={"SYSTEM.CNF": b"BOOT = cdrom:\\SLUS_010.41;1\r\n"},
                )
            )
            self.assertEqual(extract_psx_disc_serial(psx_iso), "SLUS-01041")

            # 2. Verify plain-text `inspect` (`_has_module("rich") == False`) reports psxpackager merge
            inspect_dir = root / "psx_set"
            inspect_dir.mkdir()
            for idx, code in ((1, "SLUS_010.41"), (2, "SLUS_010.80")):
                stem = f"Chrono Cross (USA) (Disc {idx})"
                bin_p = inspect_dir / f"{stem}.bin"
                cue_p = inspect_dir / f"{stem}.cue"
                bin_p.write_bytes(
                    _build_iso9660_image(
                        sector_size=2352,
                        data_offset=24,
                        sys_ident="PLAYSTATION",
                        files={"SYSTEM.CNF": f"BOOT = cdrom:\\{code};1\r\n".encode("ascii")},
                    )
                )
                cue_p.write_text(
                    f'FILE "{stem}.bin" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n',
                    encoding="utf-8",
                )

            buf = io.StringIO()
            with (
                patch("main._has_module", return_value=False),
                patch("sys.stdout", buf),
            ):
                rc = cli_main.run_inspect([inspect_dir], cfg)

            self.assertEqual(rc, 0)
            output = buf.getvalue()
            self.assertIn("psxpackager merge (2 discs -> Chrono Cross (USA).pbp)", output)

    def test_dolphin_ciso_and_wbfs_unpack_to_rvz_without_accurate_size_crash(self) -> None:
        dolphin = self.base_config.binaries.dolphin_tool
        if not dolphin:
            self.skipTest("Requires dolphin-tool / DolphinTool")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._make_isolated_config(root, PostProcessAction.MOVE)

            # 1. Synthetic GameCube `.ciso` (reproduces `WIABlob.cpp:1747` DataSizeType::Accurate crash)
            ciso_path = cfg.paths.watch_dir / "Disney's PK - Out of the Shadows (USA).ciso"
            block_size = 0x200000
            ciso_hdr = bytearray(0x8000)
            ciso_hdr[0:4] = b"CISO"
            ciso_hdr[4:8] = block_size.to_bytes(4, "little")
            ciso_hdr[8] = 1
            blk0 = bytearray(block_size)
            blk0[0:6] = b"GPKE41"
            blk0[0x1C:0x20] = b"\xc2\x33\x9f\x3d"
            ciso_path.write_bytes(bytes(ciso_hdr) + bytes(blk0))

            out_gc = ingest_target(ciso_path, cfg)
            self.assertEqual(out_gc.quarantined_files, ())
            gc_rvz = cfg.paths.library_root / "gc" / "Disney's PK - Out of the Shadows (USA).rvz"
            self.assertTrue(gc_rvz.is_file())
            self.assertEqual(gc_rvz.read_bytes()[:4], b"RVZ\x01")
            self.assertEqual(list(cfg.paths.staging_dir.iterdir()), [])

            # 2. Synthetic Wii `.wbfs` (also triggers `WIABlob.cpp:1747` on Fedora dolphin-tool 2503a)
            wbfs_path = cfg.paths.watch_dir / "Super Mario Galaxy (USA).wbfs"
            wbuf = bytearray(0x400000)
            wbuf[0:4] = b"WBFS"
            wbuf[4:8] = (0x400000 // 512).to_bytes(4, "big")
            wbuf[8] = 9   # 512B hd sector
            wbuf[9] = 21  # 2 MiB wbfs sector
            wbuf[12] = 1  # disc 0 present
            wbuf[0x200:0x206] = b"RMGE01"
            wbuf[0x218:0x21C] = b"\x5d\x1c\x9e\xa3"
            wbuf[0x300:0x302] = (1).to_bytes(2, "big")
            wbuf[0x200000:0x200006] = b"RMGE01"
            wbuf[0x200018:0x20001C] = b"\x5d\x1c\x9e\xa3"
            wbfs_path.write_bytes(bytes(wbuf))

            out_wii = ingest_target(wbfs_path, cfg)
            self.assertEqual(out_wii.quarantined_files, ())
            wii_rvz = cfg.paths.library_root / "wii" / "Super Mario Galaxy (USA).rvz"
            self.assertTrue(wii_rvz.is_file())
            self.assertEqual(wii_rvz.read_bytes()[:4], b"RVZ\x01")
            self.assertEqual(list(cfg.paths.staging_dir.iterdir()), [])


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Testes do empacotador boot-v2 para Xiaomi Redmi Note 8 (ginkgo)."""
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = REPO_ROOT.parent
PACKER = REPO_ROOT / 'scripts/pack-boot-v2.py'
UEFI_DIR = WORKSPACE / 'uefi-ginkgo'
FD_PATH = UEFI_DIR / 'Build/ginkgoPkg/DEBUG_CLANGPDB/FV/SILICIUM_UEFI.fd'
UNIXNU_SCRIPTS = WORKSPACE / 'UniXNU/scripts'

sys.path.insert(0, str(UNIXNU_SCRIPTS))
from unixnu import ramdisk as ramdisk_codec  # noqa: E402

HEADER_FMT = '<8s10I16s512s32s1024sIQIIQ'


def align_pages(size, page_size=4096):
    return (size + page_size - 1) // page_size


def extract_v2_ramdisk(img_path):
    data = img_path.read_bytes()
    hdr = struct.unpack_from(HEADER_FMT, data, 0)
    magic, k_sz, k_addr, r_sz, r_addr, s_sz, _, tags, page, ver, _, _, _, _, _, rec_sz, _, hdr_sz, dtb_sz, dtb_addr = hdr
    assert magic == b'ANDROID!' and ver == 2 and hdr_sz == 1660
    r_off = page * (1 + align_pages(k_sz, page))
    return hdr, data[r_off:r_off + r_sz]


def make_synthetic_dtb(path):
    dts = path.with_suffix('.dts')
    dts.write_text('/dts-v1/;\n/ {\n\tmodel = "ginkgo test";\n};\n')
    subprocess.run(['dtc', '-I', 'dts', '-O', 'dtb', '-o', str(path), str(dts)], check=True)


def make_synthetic_fat(path, size_mib=2):
    env = dict(os.environ, MTOOLS_SKIP_CHECK='1', TZ='UTC')
    subprocess.run(
        ['mformat', '-C', '-F', '-T', str(size_mib * 2048), '-v', 'GINKGOTEST', '-N', '12345678', '-i', str(path), '::'],
        env=env, check=True, capture_output=True,
    )
    sample = path.with_name('unixnu.toml')
    sample.write_text('[ui]\ntitle = "Ginkgo Test"\n')
    subprocess.run(['mmd', '-i', str(path), '::/EFI', '::/EFI/UniXNU'], env=env, check=True, capture_output=True)
    subprocess.run(['mcopy', '-i', str(path), '-Q', '-m', str(sample), '::/EFI/UniXNU/unixnu.toml'],
                   env=env, check=True, capture_output=True)


class PackBootV2Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not FD_PATH.is_file():
            raise unittest.SkipTest(f'FD ausente para teste: {FD_PATH}')
        cls._tmp = tempfile.TemporaryDirectory(prefix='ginkgo-packv2-test-')
        cls.tmp = Path(cls._tmp.name)
        cls.dtb = cls.tmp / 'test.dtb'
        make_synthetic_dtb(cls.dtb)
        cls.fat = cls.tmp / 'test-payload.fat'
        make_synthetic_fat(cls.fat, size_mib=2)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def run_packer(self, *extra_args, output):
        cmd = [
            sys.executable, str(PACKER),
            '--uefi', str(UEFI_DIR),
            '--fd', str(FD_PATH),
            '--dtb', str(self.dtb),
            '-o', str(output),
            *extra_args,
        ]
        return subprocess.run(cmd, capture_output=True, text=True)

    def test_default_none_compression_and_offset(self):
        out = self.tmp / 'default-none.img'
        res = self.run_packer('--ramdisk-compression', 'none', '--ramdisk-offset', '0x01000000',
                              '--ramdisk', str(self.fat), output=out)
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        hdr, ramdisk_bytes = extract_v2_ramdisk(out)
        self.assertEqual(hdr[4], 0x01000000)
        self.assertEqual(ramdisk_bytes, self.fat.read_bytes())
        report = json.loads(out.with_suffix('.json').read_text())
        self.assertEqual(report['ramdisk_encoding'], 'none')
        self.assertEqual(report['ramdisk_decoded_bytes'], self.fat.stat().st_size)
        self.assertEqual(report['ramdisk_offset'], '0x01000000')

    def test_uefi_chunks_compression_roundtrips_with_unxnfat1(self):
        out = self.tmp / 'chunks.img'
        res = self.run_packer('--ramdisk-compression', 'uefi-chunks', '--ramdisk', str(self.fat), output=out)
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        hdr, ramdisk_bytes = extract_v2_ramdisk(out)
        self.assertTrue(ramdisk_bytes.startswith(b'UNXNFAT1'))
        report = json.loads(out.with_suffix('.json').read_text())
        self.assertEqual(report['ramdisk_encoding'], 'uefi-chunks')
        self.assertEqual(report['ramdisk_decoded_bytes'], self.fat.stat().st_size)
        encoded = self.tmp / 'extracted.packed'
        decoded = self.tmp / 'extracted.fat'
        encoded.write_bytes(ramdisk_bytes)
        ramdisk_codec.decode(encoded, decoded, UEFI_DIR / 'Mu_Basecore')
        self.assertEqual(decoded.read_bytes(), self.fat.read_bytes())

    def test_custom_ramdisk_offset_0x02200000(self):
        out = self.tmp / 'high-offset.img'
        res = self.run_packer('--ramdisk-offset', '0x02200000', '--ramdisk-compression', 'uefi-chunks',
                              '--ramdisk', str(self.fat), output=out)
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        hdr, ramdisk_bytes = extract_v2_ramdisk(out)
        self.assertEqual(hdr[4], 0x02200000)
        self.assertTrue(ramdisk_bytes.startswith(b'UNXNFAT1'))
        report = json.loads(out.with_suffix('.json').read_text())
        self.assertEqual(report['ramdisk_offset'], '0x02200000')

    def test_rejects_abl_dtb_collision_and_hyp_overflow(self):
        big_incompressible = self.tmp / 'big-random.fat'
        big_incompressible.write_bytes(os.urandom(16 * 1024 * 1024))

        # 1) Colisão com DTB partindo de 0x01000000 com 16 MiB (> 15 MiB)
        res1 = self.run_packer('--ramdisk-compression', 'none', '--ramdisk-offset', '0x01000000',
                               '--ramdisk', str(big_incompressible), output=self.tmp / 'bad1.img')
        self.assertNotEqual(res1.returncode, 0, 'deveria rejeitar invasão sobre dtb_offset 0x01f00000')
        self.assertIn('dtb', (res1.stderr + res1.stdout).lower(), res1.stderr)

        # 2) Sobreposição direta em dtb_offset (0x01f00000)
        res2 = self.run_packer('--ramdisk-offset', '0x01f00000',
                               '--ramdisk', str(self.fat), output=self.tmp / 'bad2.img')
        self.assertNotEqual(res2.returncode, 0, 'deveria rejeitar sobreposição direta com DTB')
        self.assertIn('dtb', (res2.stderr + res2.stdout).lower(), res2.stderr)

        # 3) Extrapolação do limite HYP (0x05700000)
        res3 = self.run_packer('--ramdisk-compression', 'none', '--ramdisk-offset', '0x05600000',
                               '--ramdisk', str(self.fat), output=self.tmp / 'bad3.img')
        self.assertNotEqual(res3.returncode, 0, 'deveria rejeitar invasão da região HYP (0x05700000)')
        self.assertIn('hyp', (res3.stderr + res3.stdout).lower(), res3.stderr)


if __name__ == '__main__':
    unittest.main()

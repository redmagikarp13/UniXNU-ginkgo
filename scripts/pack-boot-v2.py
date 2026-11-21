#!/usr/bin/env python3
"""Empacota boot-v2.img determinístico para Xiaomi Redmi Note 8 (ginkgo)."""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import tomllib
import zlib

HEADER_FMT = '<8s10I16s512s32s1024sIQIIQ'
HEADER_V2_SIZE = 1660
PAGE_SIZE = 4096
BASE_ADDR = 0x00000000
KERNEL_OFFSET = 0x00008000
DEFAULT_RAMDISK_OFFSET = 0x01000000
TAGS_OFFSET = 0x00000100
DTB_OFFSET = 0x01F00000
ABL_BOOT_REGION_LIMIT = 0x05700000
OS_VERSION = '11.0.0'
OS_PATCH_LEVEL = '2022-02'

UNXNFAT_MAGIC = b'UNXNFAT1'
UNXNFAT_CHUNK = 1024 * 1024
UNXNFAT_HEADER = struct.Struct('<8sQII')
UNXNFAT_ENTRY = struct.Struct('<II')


def align_pages(size, page_size=PAGE_SIZE):
    """Retorna a quantidade de páginas necessária para comportar size bytes."""
    return (size + page_size - 1) // page_size


def find_tool(name, clang_bin=None, uefi=None):
    """Localiza binários LLVM na ordem recomendada para ginkgo."""
    candidates = []
    if clang_bin:
        candidates.append(Path(clang_bin))
    if os.environ.get('CLANG_BIN'):
        candidates.append(Path(os.environ['CLANG_BIN']))
    if uefi:
        candidates.append(uefi.resolve().parent / 'UniXNU/.cache/edk2-llvm22')
    candidates.append(Path('/opt/homebrew/opt/llvm@22/bin'))
    for directory in candidates:
        path = directory / name
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    resolved = shutil.which(name)
    if resolved:
        return resolved
    raise SystemExit(f'Ferramenta não encontrada: {name} (tente --clang-bin ou CLANG_BIN)')


def find_tiano_compress(uefi):
    """Localiza TianoCompress do BaseTools do EDK II."""
    resolved = shutil.which('TianoCompress')
    candidates = [Path(resolved)] if resolved else []
    uefi_root = uefi.resolve()
    candidates += [
        uefi_root / 'Mu_Basecore/BaseTools/Source/C/bin/TianoCompress',
        uefi_root / 'BaseTools/Source/C/bin/TianoCompress',
        uefi_root / 'Common/Mu/BaseTools/Source/C/bin/TianoCompress',
        uefi_root.parent / 'UniXNU/Mu_Basecore/BaseTools/Source/C/bin/TianoCompress',
    ]
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    raise SystemExit('TianoCompress não encontrado; compile Mu_Basecore BaseTools para usar --ramdisk-compression uefi-chunks')


def encode_uefi_chunks(source_path, scratch, uefi):
    """Empacota imagem FAT em formato UNXNFAT1 com blocos Tiano de 1 MiB para UniXNURamDiskDxe."""
    size = source_path.stat().st_size
    if size == 0 or size % 512 != 0 or size > 1024**3:
        raise SystemExit(f'Ramdisk para uefi-chunks precisa ser múltiplo de 512 bytes (até 1 GiB): {size}')
    compressor = find_tiano_compress(uefi)
    count = (size + UNXNFAT_CHUNK - 1) // UNXNFAT_CHUNK
    chunk_in = scratch / 'fat-chunk.bin'
    chunk_out = scratch / 'fat-chunk.compressed'
    encoded = scratch / 'ramdisk.unxnfat1'
    entries = []
    with source_path.open('rb') as incoming, encoded.open('wb') as out_stream:
        out_stream.write(UNXNFAT_HEADER.pack(UNXNFAT_MAGIC, size, UNXNFAT_CHUNK, count))
        out_stream.write(bytes(UNXNFAT_ENTRY.size * count))
        while data := incoming.read(UNXNFAT_CHUNK):
            chunk_in.write_bytes(data)
            subprocess.run([compressor, '-e', '--uefi', '-q', '-o', str(chunk_out), str(chunk_in)],
                           check=True, capture_output=True)
            compressed = chunk_out.read_bytes()
            entries.append((out_stream.tell(), len(compressed)))
            out_stream.write(compressed)
            out_stream.write(bytes(-out_stream.tell() % 8))
        out_stream.seek(UNXNFAT_HEADER.size)
        for entry in entries:
            out_stream.write(UNXNFAT_ENTRY.pack(*entry))
    return encoded.read_bytes()


def validate_dtb(blob, source):
    """Valida cabeçalho FDT básico (aceita multi-DTB concatenado do ABL)."""
    if len(blob) < 40 or blob[:4] != b'\xd0\x0d\xfe\xed':
        raise SystemExit(f'DTB inválido em {source}: magic FDT ausente')
    total_size = struct.unpack_from('>I', blob, 4)[0]
    if total_size < 40 or total_size > len(blob):
        raise SystemExit(f'DTB inválido em {source}: totalsize={total_size} fora de {len(blob)} bytes')
    return blob


def extract_v2_dtb(stock_boot):
    """Extrai a seção DTB em modo somente-leitura de uma imagem Android boot v2."""
    data = stock_boot.read_bytes()
    if len(data) < HEADER_V2_SIZE:
        raise SystemExit(f'{stock_boot}: imagem menor que o cabeçalho v2')
    hdr = struct.unpack_from(HEADER_FMT, data, 0)
    magic, k_sz, _, r_sz, _, s_sz, _, _, page, ver, _, _, _, _, _, rec_sz, _, hdr_sz, dtb_sz, _ = hdr
    if magic != b'ANDROID!' or ver != 2 or hdr_sz != HEADER_V2_SIZE or page == 0 or dtb_sz == 0:
        raise SystemExit(f'{stock_boot}: não é uma imagem Android boot v2 válida com DTB')
    pages = 1 + align_pages(k_sz, page) + align_pages(r_sz, page) + align_pages(s_sz, page) + align_pages(rec_sz, page)
    dtb_off = page * pages
    if dtb_off + dtb_sz > len(data):
        raise SystemExit(f'{stock_boot}: seção DTB truncada')
    return validate_dtb(data[dtb_off:dtb_off + dtb_sz], stock_boot)


def load_uefi_config(uefi):
    """Lê [uefi_fd] e [boot_shim] de Resources/Configs/ginkgo.toml."""
    config_path = uefi / 'Resources/Configs/ginkgo.toml'
    if not config_path.is_file():
        raise SystemExit(f'Configuração ausente: {config_path}')
    cfg = tomllib.loads(config_path.read_text())
    fd_cfg = cfg.get('uefi_fd', {})
    base, size = fd_cfg.get('base'), fd_cfg.get('size')
    if base is None or size is None:
        raise SystemExit(f'{config_path}: [uefi_fd] precisa de base e size')
    requires_header = bool(cfg.get('boot_shim', {}).get('requires_kernel_header', True))
    return base, size, requires_header


def compile_shim(uefi, base, size, requires_header, scratch, clang_bin=None):
    """Compila BootShim.S sem literal pool separado do _Payload e valida header ARM64."""
    source = uefi / 'BootShim/BootShim.S'
    if not source.is_file():
        raise SystemExit(f'Fonte BootShim ausente: {source}')
    clang = find_tool('clang', clang_bin, uefi)
    objcopy = find_tool('llvm-objcopy', clang_bin, uefi)
    nm = find_tool('llvm-nm', clang_bin, uefi)
    obj = scratch / 'shim.o'
    shim_bin = scratch / 'shim.bin'
    subprocess.run(
        [clang, '--target=aarch64-none-elf', '-c', f'-DFD_BASE={base}', f'-DFD_SIZE={size}',
         f'-DREQUIRES_KERNEL_HEADER={int(requires_header)}', str(source), '-o', str(obj)],
        check=True,
    )
    subprocess.run([objcopy, '-O', 'binary', str(obj), str(shim_bin)], check=True)
    symbols = subprocess.check_output([nm, '-n', str(obj)], text=True)
    payload_off = next((int(line.split()[0], 16) for line in symbols.splitlines() if line.endswith(' _Payload')), None)
    shim = shim_bin.read_bytes()
    if payload_off != len(shim):
        raise SystemExit(f'BootShim inválido: _Payload ({payload_off}) != len(shim) ({len(shim)})')
    if requires_header and (shim[56:60] != b'ARM\x64' or struct.unpack_from('<QQ', shim, 8) != (base, len(shim) + size)):
        raise SystemExit('Cabeçalho ARM64 do BootShim não corresponde a FD_BASE/FD_SIZE + shim')
    return shim


def validate_ramdisk_layout(ramdisk_offset, ramdisk_size, dtb_size):
    """Garante que o ramdisk não colida com o DTB nem invada a região HYP do ABL."""
    if not ramdisk_size:
        return
    if ramdisk_offset < 0 or ramdisk_offset % PAGE_SIZE != 0:
        raise SystemExit(f'--ramdisk-offset inválido (precisa ser alinhado a 4 KiB e >= 0): {ramdisk_offset:#x}')
    ramdisk_end = ramdisk_offset + ramdisk_size
    dtb_end = DTB_OFFSET + dtb_size
    if ramdisk_offset < dtb_end and ramdisk_end > DTB_OFFSET:
        raise SystemExit(
            f'Ramdisk [{ramdisk_offset:#x}..{ramdisk_end:#x}) colide com a região DTB [{DTB_OFFSET:#x}..{dtb_end:#x}); '
            'use --ramdisk-offset 0x02200000 ou reduza o ramdisk'
        )
    if ramdisk_end > ABL_BOOT_REGION_LIMIT:
        raise SystemExit(
            f'Ramdisk [{ramdisk_offset:#x}..{ramdisk_end:#x}) invade a região HYP em {ABL_BOOT_REGION_LIMIT:#x}'
        )


def verify_v2_image(path, combined, dtb_bytes, ramdisk_bytes, ramdisk_offset):
    """Valida todos os campos do cabeçalho v2 e faz round-trip do payload inteiro."""
    data = path.read_bytes()
    if len(data) < PAGE_SIZE:
        raise SystemExit('Imagem gerada menor que 1 página')
    hdr = struct.unpack_from(HEADER_FMT, data, 0)
    magic, k_sz, k_addr, r_sz, r_addr, s_sz, _, tags, page, ver, osver, _, _, _, _, rec_sz, _, hdr_sz, dtb_sz, dtb_addr = hdr
    expected_r_addr = (BASE_ADDR + ramdisk_offset) if ramdisk_bytes else 0
    if (magic != b'ANDROID!' or ver != 2 or hdr_sz != HEADER_V2_SIZE or page != PAGE_SIZE or
            k_addr != BASE_ADDR + KERNEL_OFFSET or r_addr != expected_r_addr or
            tags != BASE_ADDR + TAGS_OFFSET or dtb_addr != BASE_ADDR + DTB_OFFSET or
            s_sz != 0 or rec_sz != 0):
        raise SystemExit(f'Cabeçalho v2 inesperado em {path}: ver={ver} hdr={hdr_sz} page={page} r_addr={r_addr:#x}')
    k_off = page
    r_off = k_off + align_pages(k_sz, page) * page
    dtb_off = r_off + align_pages(r_sz, page) * page
    kernel_blob = data[k_off:k_off + k_sz]
    unpacker = zlib.decompressobj(16 + zlib.MAX_WBITS)
    if unpacker.decompress(kernel_blob) != combined or not unpacker.eof or unpacker.unused_data != b'':
        raise SystemExit('Round-trip gzip de BootShim + FD falhou (ou há DTB anexado indevidamente)')
    if data[r_off:r_off + r_sz] != ramdisk_bytes:
        raise SystemExit('Round-trip de ramdisk falhou')
    if data[dtb_off:dtb_off + dtb_sz] != dtb_bytes:
        raise SystemExit('Round-trip de DTB separado falhou')
    return {'header_version': ver, 'header_size': hdr_sz, 'pagesize': page, 'os_version_raw': hex(osver),
            'kernel_bytes': k_sz, 'ramdisk_bytes': r_sz, 'dtb_bytes': dtb_sz}


def sha256_bytes(blob):
    return hashlib.sha256(blob).hexdigest()


def parse_offset(value):
    return int(value, 0)


def main():
    repo_root = Path(__file__).resolve().parent.parent
    default_uefi = repo_root.parent / 'uefi-ginkgo'
    if not default_uefi.is_dir():
        default_uefi = Path('../uefi-ginkgo')

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--uefi', type=Path, default=default_uefi, help='Checkout do Mu-Silicium (default: ../uefi-ginkgo)')
    parser.add_argument('--fd', type=Path, help='Firmware volume SILICIUM_UEFI.fd')
    dtb_group = parser.add_mutually_exclusive_group(required=True)
    dtb_group.add_argument('--dtb', type=Path, help='Arquivo DTB v2 para --dtb de mkbootimg.py')
    dtb_group.add_argument('--stock-boot', type=Path, help='Imagem stock v2 para extrair a seção DTB em somente-leitura')
    parser.add_argument('--ramdisk', type=Path, help='Ramdisk FAT (omita ou use vazio se não houver)')
    parser.add_argument('--ramdisk-compression', choices=('none', 'uefi-chunks'), default='none',
                        help='Codificação do ramdisk (default: none)')
    parser.add_argument('--ramdisk-offset', type=parse_offset, default=DEFAULT_RAMDISK_OFFSET,
                        help='Offset físico do ramdisk acima de base 0x40000000 (default: 0x01000000)')
    parser.add_argument('--clang-bin', type=Path, help='Diretório com clang/llvm-objcopy/llvm-nm')
    parser.add_argument('-o', '--output', required=True, type=Path, help='Caminho de saída do boot-v2.img')
    args = parser.parse_args()

    uefi = args.uefi.resolve()
    mkbootimg = uefi / 'Resources/Scripts/mkbootimg.py'
    if not mkbootimg.is_file():
        raise SystemExit(f'mkbootimg.py ausente: {mkbootimg}')
    fd_path = (args.fd or (uefi / 'Build/ginkgoPkg/DEBUG_CLANGPDB/FV/SILICIUM_UEFI.fd')).resolve()
    if not fd_path.is_file():
        raise SystemExit(f'Firmware volume não encontrado: {fd_path}')

    base, fd_size, requires_header = load_uefi_config(uefi)
    fd_bytes = fd_path.read_bytes()
    if len(fd_bytes) != fd_size:
        raise SystemExit(f'Tamanho do FD difere de ginkgo.toml: {len(fd_bytes):#x} != {fd_size:#x}')

    dtb_bytes = extract_v2_dtb(args.stock_boot) if args.stock_boot else validate_dtb(args.dtb.read_bytes(), args.dtb)
    if args.ramdisk and not args.ramdisk.is_file():
        raise SystemExit(f'Ramdisk não encontrado: {args.ramdisk}')
    decoded_bytes = args.ramdisk.stat().st_size if args.ramdisk else 0

    out = args.output.resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix='ginkgo-bootv2-', dir=out.parent) as scratch_dir:
        scratch = Path(scratch_dir)
        if not args.ramdisk or decoded_bytes == 0:
            ramdisk_bytes = b''
            encoding = 'none'
        elif args.ramdisk_compression == 'uefi-chunks':
            ramdisk_bytes = encode_uefi_chunks(args.ramdisk, scratch, uefi)
            encoding = 'uefi-chunks'
        else:
            ramdisk_bytes = args.ramdisk.read_bytes()
            encoding = 'none'

        validate_ramdisk_layout(args.ramdisk_offset, len(ramdisk_bytes), len(dtb_bytes))

        shim = compile_shim(uefi, base, fd_size, requires_header, scratch, args.clang_bin)
        combined = shim + fd_bytes
        kernel_gz = scratch / 'Image.gz'
        kernel_gz.write_bytes(gzip.compress(combined, mtime=0))
        dtb_file = scratch / 'v2.dtb'
        dtb_file.write_bytes(dtb_bytes)
        ramdisk_file = scratch / 'ramdisk'
        ramdisk_file.write_bytes(ramdisk_bytes)
        candidate = scratch / out.name

        cmd = [
            'python3', str(mkbootimg),
            '--header_version', '2',
            '--base', f'{BASE_ADDR:#010x}',
            '--kernel_offset', f'{KERNEL_OFFSET:#010x}',
            '--ramdisk_offset', f'{args.ramdisk_offset:#010x}',
            '--tags_offset', f'{TAGS_OFFSET:#010x}',
            '--dtb_offset', f'{DTB_OFFSET:#010x}',
            '--pagesize', str(PAGE_SIZE),
            '--os_version', OS_VERSION,
            '--os_patch_level', OS_PATCH_LEVEL,
            '--kernel', str(kernel_gz),
            '--dtb', str(dtb_file),
            '--ramdisk', str(ramdisk_file),
            '-o', str(candidate),
        ]
        subprocess.run(cmd, check=True)
        report = verify_v2_image(candidate, combined, dtb_bytes, ramdisk_bytes, args.ramdisk_offset)
        report.update(
            image=str(out),
            image_bytes=candidate.stat().st_size,
            image_sha256=sha256_bytes(candidate.read_bytes()),
            fd_sha256=sha256_bytes(fd_bytes),
            shim_bytes=len(shim),
            shim_sha256=sha256_bytes(shim),
            dtb_sha256=sha256_bytes(dtb_bytes),
            ramdisk_sha256=sha256_bytes(ramdisk_bytes),
            ramdisk_offset=f'{args.ramdisk_offset:#010x}',
            ramdisk_encoding=encoding,
            ramdisk_decoded_bytes=decoded_bytes,
        )
        os.replace(candidate, out)

    out.with_suffix('.json').write_text(json.dumps(report, indent=2) + '\n')
    print(f'boot-v2: {out} ({report["image_bytes"]} bytes, sha256={report["image_sha256"]})')


if __name__ == '__main__':
    main()

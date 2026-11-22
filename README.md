# UniXNU — Xiaomi Redmi Note 8 (`ginkgo`, 4 GB / 64 GB)

Port experimental do **UniXNU** (`ipados-17.7.11-21H461`, `arm64`, páginas de **4 KiB**) para o **Xiaomi Redmi Note 8 (`ginkgo`)**, baseado no SoC **Qualcomm Snapdragon 665 (`SM6125`)**.

> ⚠️ **Aviso de hardware:** Este projeto está em desenvolvimento ativo. Os testes de kernel são feitos via `fastboot boot` em RAM; apenas a partição `userdata` recebe a imagem `SHNGROOT` v2 (`OS` + `SystemOS`) e o overlay persistente `SHNGDATA`. Partições críticas (`boot`, `recovery`/TWRP, `abl`, `xbl`, GPT) não são modificadas.

---

## Hardware Alvo (Variante Testada)

| Componente | Especificação |
| :--- | :--- |
| **Dispositivo** | Xiaomi Redmi Note 8 (`ginkgo`) |
| **Memória / Armazenamento** | **4 GB LPDDR4X / 64 GB eMMC 5.1** (Micron `S0J9K8`, `122,142,720` setores de 512 B) |
| **SoC / CPU** | Qualcomm `SM6125` (8x Kryo 260: 4x Cortex-A73 + 4x Cortex-A53) |
| **ISA / Page Size** | **ARMv8.0-A** (sem `PAN`, sem `LSE`, sem `PAC`) — páginas de **4 KiB** |
| **Bootloader (ABL)** | MIUI 12.5 Android 11 `boot.img` **header v2** com DTB separado (`stock-v2-dtb.bin`) e shim `ARM64` + `UEFI FD` (`uefi-ginkgo`) |
| **Display (SimpleFB)** | `1080×2340` @ 32 bpp (`0x5C000000`, stride `4320` bytes) |

---

## Estado Atual dos Marcos (Validado em Silício Real)

| Marco | Escopo | Estado |
| :--- | :--- | :--- |
| **Marco 1** | Boot `boot-v2.img` via ABL → Shim → Mu-Silicium UEFI (`uefi-ginkgo`) → `DarwinBoot` + ramdisk FAT (`uefi-chunks`) | ✅ Validado |
| **Marco 2** | Entrada no XNU `mach.development.bcm2837` (4K, ARMv8.0-A), `arm_vm_init`, SMP 8 núcleos, `PE_create_console`, `ApplePMGRNub` (`pmgr=2`) | ✅ Validado |
| **Marco 3A** | Driver eMMC SDCC1 (`Sm6125EmmcCore`): `GCC` + `SDHCI-MSM v5`, DDR52 8-bit, 32-bit ADMA2 (~23 MB/s), GPT real (87 partições) | ✅ Validado |
| **Marco 3B** | `IOBlockStorageDevice` (`Sm6125EmmcBlock`) + `VerifiedRoot` RSA-3072 + `SHNGROOT` v2 (`OS` + `SystemOS` union mount em `/`) | ✅ Validado |
| **Marco 4A** | `NativeRamBlock` (seed APFS `Data` de 4 GiB) + `SHNGDATA` persistente em eMMC (`CMD24`/`CMD25` + trava LBA) — `/private/var` RW sobrevivendo a reboot | ✅ Validado |
| **Marco 4B** | Bringup do userspace `/sbin/launchd` com `ipsw-trust.bundle` (`2.554` hashes) e política `disabled.plist` (86 processos simultâneos em 180s) | ✅ Validado |
| **Marco 5** | Display GOP (`NativeLegacyDisplay` 1080×2340, `IOMobileFramebufferLegacy` ABI 21H461), SMP 8/8 núcleos (`PSCI` + GICv3), `AppleKeyStoreState`, 2,81 GiB RAM (`0xb4000000`), skip-setup/8-bit FB no `/private/var` — Lock Screen do `SpringBoard` renderizada em silício real | ✅ Validado |

### Detalhes da Verificação de Persistência (Marco 4A)
- A imagem `userdata-root.img` (`SHNGROOT` v2, `9,457,106,944` B) ocupa o início de `userdata` (`disk0s87`, LBAs `0x01298000..0x0747BFDE`).
- O driver C (`Sm6125EmmcCore.c`) impõe uma **trava rígida de LBA**: qualquer escrita `CMD24`/`CMD25` é recusada (`SM6125_EMMC_RC_WRITE_PROTECTED`) se tentar tocar MBR, GPT, partições `disk0s1..disk0s86`, `SHNGROOT` v2 ou a zona de guarda de **4 MiB** (`8192` setores) após o `SHNGROOT`.
- O overlay `SHNGDATA` opera na cauda livre de `userdata` (`LBA >= 0x2437800`), com verificação CRC32 da GPT primária antes e depois das escritas (`gpt=1/1/1`).

---

## Estrutura dos Repositórios

- **`UniXNU-ginkgo`** (este repositório): `board.toml`, `configs/ginkgo_defconfig`, `dts/ginkgo.dts`, `scripts/pack-boot-v2.py` e testes de empacotamento.
- **`UniXNU-sm6125-common`**: camada comum do SoC `SM6125` (`Sm6125EmmcCore`, `Sm6125ShngRoot`, `Sm6125EmmcBlock`, `Sm6125PlatformPower`, patches XNU e suítes ASan+UBSan).
- **`uefi-ginkgo`** (branch `ginkgo-unixnu` de `Mu-Silicium`): firmware UEFI adaptado para `ARMv8.0-A` e mapa de memória compatível com o ABL do `ginkgo`.

---

## Como Compilar e Testar

### 1. Pré-requisitos
- macOS com Xcode (`iphoneos` SDK), Homebrew (`dtc`, `mtools`, `llvm@22`), checkout do `UniXNU` e `uefi-ginkgo` em diretórios irmãos.
- Para imagens `userdata` > 2 GiB no `fastboot` do macOS, converter para Android sparse via `UniXNU/scripts/make-android-sparse.py`.

### 2. Build do kernel e prelink
```bash
export DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer
cd UniXNU-ginkgo
../UniXNU/unixnu tree
../UniXNU/unixnu kernel
../UniXNU/unixnu prelink
```

### 3. Testes locais
```bash
python3 -m unittest discover -s tests -v
```
(Veja também os testes de SoC e ASan+UBSan em `UniXNU-sm6125-common`.)

### 4. Empacotar `boot-v2` e subir em RAM
```bash
../UniXNU/unixnu payload \
  --unique-chip-id <CHIP_ID_HEX> \
  --trust-cache <PATH_TO_IPSW_TRUST_BUNDLE> \
  --data-ramdisk <PATH_TO_DATA_4G_DBR> \
  --kernel-arg sm6125-emmc-block=3 \
  --kernel-arg sm6125-data-persist=1

python3 scripts/pack-boot-v2.py \
  --uefi ../uefi-ginkgo \
  --dtb <PATH_TO_STOCK_V2_DTB> \
  --ramdisk ../UniXNU/out/ginkgo/ipados-17.7.11-21H461/ginkgo/payload/payload.img \
  --ramdisk-compression uefi-chunks \
  --ramdisk-offset 0x01000000 \
  -o boot-v2-test.img

fastboot boot boot-v2-test.img
```

---

## Licença
Consulte os cabeçalhos SPDX de cada arquivo (`MIT` / `BSD-2-Clause-Patent` conforme componente). Nenhum binário proprietário da Apple ou da Xiaomi é redistribuído neste repositório.

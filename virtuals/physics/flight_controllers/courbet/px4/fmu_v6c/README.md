# PX4 FMUv6C + QEMU + FastDyn P2

사용자가 제공한 `courbet/bin/px4_fmu-v6c_default.elf`를 Cortex-M7에서 실행하는
ARM/NuttX 프로파일이다. 호스트용 PX4 SITL 실행 파일로 대체하지 않는다.
확인한 ELF의 PX4 커밋은 `0b6e4687defb353a34201951809efd3f0040a9ba`
(`v1.17.0-rc2`)이며, 실행 시 `ver all`에서도 이 값이 출력된다.

## 실행

FastDyn 디렉터리에서 준비된 ELF 주소 설정과 플러그인을 사용해 실행한다:

```bash
fastdyn run -c configs/px4_fmu_v6c_fire_p2.toml
```

TOML의 `[Run].bridge_boot`에 지정된 `boot.nsh`를 자동으로 실행한다.
`FASTDYN_BRIDGE_BOOT`를 미리 export할 필요가 없다. NSH 프롬프트마다 명령을
하나씩 전달해 센서 발행 모듈, EKF2, PWM 출력, commander와 멀티콥터 제어기를
시작한다. 이어서 MAVProxy/MAVCesium과 PX4 미션 도우미가 자동 실행되어
기본 미션을 업로드하고 **정상 arming → 이륙 → 경유점 이동 → 착륙 → disarm**을
진행한다. 브라우저에서 `http://127.0.0.1:5000/mavcesium/`을 열면 위치와 자세를
볼 수 있다. 미션 완료 시 실행 세션과 도우미가 종료된다.
`FASTDYN_PX4_READY` 뒤에는 `ver all`, `listener sensor_accel -n 1`,
`ekf2 status` 등을 터미널에서 사용할 수 있다. Ctrl-C로 종료한다.
`boot.nsh` 자체는 arming을 요청하지 않는다. 자동 arming은 별도 호스트 미션
도우미가 담당한다. `FASTDYN_PX4_READY`는 부트 명령 전달 완료를 뜻하며,
EKF 준비나 arming 성공을 의미하지 않는다.

ELF의 심볼·DWARF ABI·명령어를 검사하고 `unlabeled_conf`를 다시 생성한 다음
플러그인을 빌드하여 실행하려면 기존 스크립트를 사용한다:

```bash
./virtuals/physics/flight_controllers/courbet/px4/fmu_v6c/run.sh -o out/px4-fmu-v6c
```

이 스크립트는 공통 설정 생성기와 `ninja -C build`를 실행한다. 기본 Python/CLI는
`fastdyn-env/bin`을 사용한다. 필요하면 `FASTDYN_PYTHON`, `FASTDYN_CLI`로
경로를 지정한다. GDB의 Python 지원과 pyelftools가 필요하다.
이미 FMU를 빌드했다면 실행 인자에 `--no-build-fmu`를 추가할 수 있다.

모듈을 자동 시작하지 않고 NuttX 콘솔만 열려면 빈 환경 변수로 기본값을 덮어쓴다:

```bash
FASTDYN_BRIDGE_BOOT= fastdyn run -c configs/px4_fmu_v6c_fire_p2.toml --no-run-processes
# 또는 빌드/설정 생성 스크립트의 기존 옵션:
./virtuals/physics/flight_controllers/courbet/px4/fmu_v6c/run.sh --shell -o out/px4-shell
```

다른 NSH 명령 파일을 쓰려면 `FASTDYN_BRIDGE_BOOT=/absolute/path/custom.nsh`를
지정한다. 환경 변수는 TOML보다 우선하며, 환경 변수의 상대 경로는 실행한
디렉터리 기준이다. TOML의 상대 경로는 FastDyn 저장소 루트 기준으로 해석한다.
부트 파일 설정은 실행 세션 동안만 적용하므로 이후 다른 프로파일에 남지 않는다.
`--no-run-processes`는 호스트 도우미만 끄며 PX4 자동 초기화는 유지한다.
명령은 한 줄에 126자 이하이고 줄바꿈으로 끝나야 한다.

## 자동 미션과 arming

기본 실행에서는 다음 단계가 자동으로 진행된다.

1. `dataman start -r`로 RAM 미션 저장소, `navigator`로 미션 실행기를 시작한다.
   센서/EKF/commander/멀티콥터 제어기와 TELEM1 MAVLink도 부트 파일에서 시작한다.
2. MAVProxy가 UART 브리지에 GCS heartbeat를 보내고, PX4 응답을 도우미와 뷰어에 전달한다.
3. `mav_command_control_px4.py`가 PX4 heartbeat를 확인하고 파라미터와 미션을 업로드한다.
   기본 QGC 파일의 home 행은 제거하여 실제 비행 항목 5개를 전달한다.
4. GPS fix, 전역 위치와 EKF 유효성 메시지가 준비될 때까지 기다린다.
5. `MAV_CMD_DO_SET_MODE`로 AUTO.MISSION을 요청하고 heartbeat의 PX4 main/submode를 확인한다.
6. `MAV_CMD_COMPONENT_ARM_DISARM(1, 0)`으로 정상 arm을 요청하고 heartbeat의 armed 비트를 확인한다.
7. `MAV_CMD_MISSION_START`의 승인 ACK를 확인한다. 비행, 마지막 항목, 착륙과 disarm까지
   확인되어야 성공으로 종료한다.

**AUTO.MISSION 모드 전환만으로는 arming되지 않는다.** 이 ELF의 PX4 Commander는
유효한 미션에 대한 `MAV_CMD_MISSION_START` 처리 자체에서도 모드 전환과 정상 arm을
시도한다. 호스트 도우미는 상태 전이를 분명하게 검증하기 위해 arm을 명시적으로 먼저
요청한다. Force-arm 값 `21196`이나 arming 판정 우회는 사용하지 않는다.
일시적인 거절은 제한 시간 내 재시도하며, 준비 상태·업로드·arm·완료 실패는 오류로
보고하고 실행 세션을 종료한다. 구체적인 거절 사유는 콘솔과 MAVLink STATUSTEXT에 표시된다.

모듈과 UART만 시작하고 자동 미션 없이 진단하려면:

```bash
fastdyn run -c configs/px4_fmu_v6c_fire_p2.toml --no-run-processes
```

이 경우 MAVProxy와 뷰어도 실행하지 않는다. 다른 터미널에서 브리지에 직접 연결하여
업로드만 시험할 수 있다. `--no-arm`은 모드 변경·arming·미션 시작을 요청하지 않는다.

```bash
fastdyn-env/bin/python virtuals/physics/flight_controllers/courbet/mavlink/mav_command_control_px4.py \
  --connect udpout:127.0.0.1:14551 --no-arm \
  virtuals/physics/flight_controllers/courbet/mavlink/px4_init.param \
  virtuals/physics/flight_controllers/courbet/mavlink/copter_mission.waypoints
```

기본 미션/파라미터 파일은 `FASTDYN_MISSION_FILE`, `FASTDYN_PARAM_FILE`로 바꿀 수 있다.
RC 입력이 없는 자동 미션 구성이므로 `COM_RC_IN_MODE=4`를 부트 단계에서 설정한다.
SD 카드 검사 및 로그 설정도 이 가상 보드에 맞추지만, EKF 유효성과 정상 arming
판단은 실제 PX4 코드가 수행한다.

## TELEM1 UART와 호스트 UDP

```text
PX4 commander / navigator / uORB
             ↕ 실제 PX4 mavlink 모듈
NuttX TELEM1: /dev/ttyS5 (UART7, g_uart7priv)
             ↕ FastDyn UART 어댑터, 원본 MAVLink 바이트
호스트 UDP 127.0.0.1:14551
             ↕ MAVProxy --master=udpout:127.0.0.1:14551
             ├─ --out=udpout:127.0.0.1:14552 ↔ PX4 미션 도우미(udpin)
             └─ fastdyn_cesium → HTTP/WebSocket :5000 → 브라우저
```

| 환경 변수 | 기본 포트 | 역할 |
|---|---:|---|
| `FASTDYN_MAVLINK_FIRMWARE_PORT` | UDP 14551 | QEMU 호스트 어댑터가 loopback에 bind |
| `FASTDYN_MAVLINK_GCS_PORT` | UDP 14552 | 미션 도우미가 bind, MAVProxy가 양방향 전달 |
| `FASTDYN_MAVCESIUM_PORT` | TCP 5000 | MAVCesium 웹 페이지 및 WebSocket |

부트 명령은 `mavlink start -d /dev/ttyS5 -b 921600 -m 0 -r 100000 -Z`와
`mavlink boot_complete`이다. 현재 ELF는 Normal 모드의 문자열 `normal`을 받지
않으므로 숫자 `0`을 사용한다. `-Z`는 모델링하지 않은 하드웨어 흐름 제어를 끈다.
921600은 게스트 UART 설정이며, 이 어댑터가 실제 직렬 전송시간까지 재현하지는 않는다.

송신은 `uart_write`의 파일→inode→장치 포인터로 NSH 콘솔과 TELEM1을 구분한다.
콘솔(USART3)은 계속 터미널로 출력하고 TELEM1의 바이트만 UDP로 전달한다.
수신은 nonblocking UDP 버퍼를 HRT의 UART poll 시점에 공급하여 실제 NuttX
`uart_recvchars`를 실행한다. 게스트 UART 수신 큐, semaphore와 poll을 통해
PX4 MAVLink 수신 스레드가 깨어나므로, PX4의 파라미터·미션·명령 처리가 실제로 실행된다.
호스트가 가짜 heartbeat나 승인 ACK를 생성하지 않는다.

UDP 소켓은 TELEM1이 열릴 때 생성한다. 첫 수신 패킷의 송신 주소로 응답하므로
MAVProxy 또는 직접 연결한 `udpout` 클라이언트의 heartbeat가 필요하다.
수신 전의 UART 송신은 버린다. 브리지는 하나의 현재 UDP peer를 사용하므로 여러
클라이언트를 붙일 때는 MAVProxy에서 분배한다. 포트 충돌/잘못된 포트는 명시적으로
실패하며, 소켓은 QEMU 프로세스 종료 시 닫힌다. 기본 QEMU monitor TCP 포트는
4444이며 미션 도우미의 종료 감시기가 이 포트로 QEMU 프로세스를 찾아 정상 종료시킨다.
MAVProxy의 `mav.tlog`, `mav.tlog.raw`, `mav.parm`은 실행 출력 디렉터리에 저장한다.

뷰어의 `GLOBAL_POSITION_INT`와 `ATTITUDE`는 FMU 센서 피드백을 처리한 PX4 추정값이다.
따라서 모터 출력으로 FMU가 움직이고, 센서/EKF를 거친 결과가 화면에 반영된다.
브라우저의 지도·Cesium 자산 로딩에는 외부 네트워크가 필요할 수 있다.

## 브리지 경로

```
PX4 ARM ELF / NuttX
  ├─ QEMU 가상 시간 → hrt_absolute_time, NuttX SysTick
  ├─ TIM8 HRT IRQ 62 (1 ms) ← FMU를 같은 가상 시각까지 advance
  ├─ FMU accel/gyro/mag/baro/GPS → uORB → 센서 처리 / EKF2 / 제어기
  └─ actuator_outputs_sim → 4채널 PWM → FMU P2
```

구현은 기존 `flight_controllers/ardupilot/ardupilot.c`에 있다. `px4.c`는
추가하지 않았다. 시간, `phy_*` 물리 API, FMI 실행은 공유하고, 펌웨어별 호출
규약과 데이터 구조를 어댑터로 구분한다. 기존 ArduPilot 훅의 `R0=this`, C++
객체 오프셋, SPI 데이터 표현을 PX4 함수에 그대로 적용할 수는 없다.

이 프로파일은 ELF에 포함된 `simulator_sih`와 `sensor_*_sim`을 센서 발행원으로
시작한다. `uORB::DeviceNode::publish` 진입 시 발행 데이터를 FMU 값으로
교체하며 실제 uORB 배포·NuttX 스케줄러·센서 처리·EKF2·제어 코드는 실행한다.
SIH의 자체 동역학도 실행되지만 IMU/기압/자기장/GPS 피드백은 FMU 값으로
교체된다. **SIH의 `*_groundtruth` 토픽은 FMU 상태가 아니므로 검증용으로
사용하면 안 된다.** FMU에 연결되지 않은 SIH 거리 센서는 꺼 둔다.

센서 단위는 FRD 가속도 m/s², 각속도 rad/s, 자기장 gauss, 기압 Pa,
온도 °C, GPS 위경도 deg 및 고도 m이다. FMU에 지오이드 모델이 없으므로
GPS MSL 고도와 타원체 고도를 같은 값으로 전달한다. 정지한 이상 센서가
PX4의 반복값 검출에서 STALE로 처리되지 않도록 결정론적 해시 수열의 디더를
추가한다: 가속도 최대 ±0.0001 m/s², 각속도 최대 ±0.00001 rad/s,
자기장 최대 ±0.00001 gauss, 기압 최대 ±0.1 Pa. 보정된 실제 센서 잡음 모델은 아니다.
단순 +/- 교번 수열은 VehicleIMU의 적분에서 상쇄되어 gyro STALE을 다시 발생시키므로
사용하지 않는다. 이 문제를 수정한 후 장시간 정지 상태에서도 EKF 입력이 유지된다.

모터 순서는 PX4 Quad-X의 전방 오른쪽 / 후방 왼쪽 / 전방 왼쪽 / 후방 오른쪽이다.
현재 `quad_p2.toml`과 같은 채널 순서이며 정규화 출력 0–1을 이 FMU의
1100–1900 PWM 범위로 변환한다. NaN 또는 없는 채널은 1000으로 정지시킨다.
다른 모터 순서나 PWM 범위의 FMU를 쓰려면 어댑터도 맞춰야 한다.

## 구성 파일과 하드웨어 범위

- `labeled_conf`: 심볼 이름을 사용하는 원본 훅/레지스터 수정 목록.
- `profile.toml`: 공통 생성기에서 사용할 PX4 규칙, ELF 경로, FMUv6C 벡터 주소와
  HRT/SysTick 명령어 검사 조건. 경로는 이 파일 기준으로 해석한다.
- `../../prepare_execution_configuration.py`: 공통 CLI. ELF 주소 변환·출력은
  `../../execution_configuration`, NuttX/uORB ABI 검사는 `../../profiles/px4`에서 담당한다.
  필수 심볼·명령어·구조체 검사가 실패하면 기존 생성 파일을 유지하고 중단한다.
- `unlabeled_conf`: 생성된 주소 설정, ELF SHA-256, 진단용 `symbols.txt`.
- `boot.nsh`: ARM 펌웨어에 포함된 모듈을 시작하는 NSH 명령 목록.
- `configs/px4_fmu_v6c_fire_p2.toml`: M7, 벡터 `0x08020000`, H743 RAM 뱅크와 P2 FMU.

`create_new_conf.sh`는 Rover 설정을 복사하는 템플릿 생성기이므로 PX4 주소/ABI를
변환하지 않는다. `run.sh`는 공통 생성기에 이 디렉터리의 `profile.toml`을 전달한다.
설정만 생성하려면 FastDyn 루트에서 다음 명령을 실행한다.

```bash
fastdyn-env/bin/python virtuals/physics/flight_controllers/courbet/prepare_execution_configuration.py \
  --profile virtuals/physics/flight_controllers/courbet/px4/fmu_v6c/profile.toml
```

기존 `prepare.py`는 공통 도구로 통합했다. ELF를 바꾸면 반드시 다시 실행해야 하며
다른 버전·보드는 훅과 ABI 재검토가 필요하다. `--elf`로 검사할 ELF를,
`--output-dir`로 별도 생성 디렉터리를 지정할 수 있다. 두 옵션의 경로는 현재 작업
디렉터리 기준이다. 이 옵션은 FastDyn 실행 TOML의 바이너리 경로를 변경하지 않는다.

이 구성은 **전체 Pixhawk6C/STM32H743 주변장치 에뮬레이터가 아니다.** 일반
`cortexm` 머신에서 PLL, 실제 UART/SPI/I2C 센서 초기화, USB 전원 준비,
SD/MMC/MTD 대기를 훅으로 우회한다. 레지스터 수준 센서 드라이버 시험을
하려면 해당 주변장치 모델이 추가로 필요하다. NSH 콘솔과 TELEM1 UART를 지원하며
다른 UART는 아직 연결하지 않는다. UART 장치 및 uORB 구조체 오프셋은 ELF DWARF로
검증한다. 도우미의 사용법과 ArduPilot용 도우미와의 차이는
[공통 README](../../README.md#flight-stack-specific-mission-helpers)에 있다.

NuttX SysTick은 1 ms이다. 현재 QEMU `cortexm`의 시스템 클록이 168 MHz이므로
ELF의 480 MHz용 reload를 167999로 수정한다. TIM8 HRT는 1 ms 간격으로
서비스한다. `icount`는 가상 시간 기준이며 실제 H743의 사이클 정확도를
보장하지 않는다. QEMU 클록을 바꾸면 SysTick 수정값도 바꿔야 한다.

파라미터 영구 저장소는 제공하지 않아 자동 저장 요청을 건너뛴다. 설정은
매 실행마다 `boot.nsh`에서 적용된다. SD 로그도 사용하지 않으며 SD 카드 검사만
해제한다. 초기 하드웨어 ID 읽기 실패 메시지는 ID 주변장치가 없기 때문이다.
미션 저장소도 RAM이므로 재시작하면 파라미터와 미션을 다시 적용한다.

## 검증

```bash
fastdyn-env/bin/python virtuals/physics/flight_controllers/courbet/tests/test_execution_configuration.py
fastdyn-env/bin/python virtuals/physics/flight_controllers/courbet/tests/smoke_px4_fmu_v6c.py --auto-boot
fastdyn-env/bin/python virtuals/physics/flight_controllers/courbet/tests/smoke_px4_fmu_v6c.py
fastdyn-env/bin/python virtuals/physics/flight_controllers/courbet/tests/smoke_px4_mission.py
```

첫 검사는 ArduPlane/PX4 주소 생성, 경로 해석, 누락·중복 심볼, ABI·명령어 변경,
ELF 해시 검사, CFI 플래그 및 실패 시 기존 출력 보존을 검사한다. `--auto-boot` 검사는
환경 변수를 지정하지 않은 직접 `fastdyn run`으로 실제 QEMU를 기동하여
NuttX/PX4 버전, 자동 초기화 완료와 6종 브리지 연결을 확인한다. 마지막 명령은
사용자 지정 부트 파일로 GPS 위치와 모터 시험의 1100 → 1180 → 1100 변화까지 확인한다.
모터 시험은 에뮬레이터 내부에서만 수행한다. 로그는 `out/px4-smoke-*/console.log`에
남는다. 통합 검사는 빌드된 P2 FMU가 있어야 한다.

`smoke_px4_mission.py`는 기본 TOML의 직접 실행과 기본 부트 파일을 사용한다.
실제 UART↔UDP↔MAVProxy 경로에서 INT32/REAL32 파라미터 왕복, 5개 미션 업로드,
정상 arming, 이륙·이동·착륙·disarm을 검증한다. MAVCesium HTTP 응답과 WebSocket의
위치·자세 스트림도 수신하여 고도 10 m 이상과 수평 이동 5 m 이상을 검사한다.
기본 포트는 시험 전용 19551/19552/19553이며 `--base-port`로 변경할 수 있다.
`out/px4-mission-*/`에 콘솔 로그, 뷰어 수신 데이터(`viewer.jsonl`), 측정 요약을 저장한다.
브라우저의 지도 렌더링이나 실제 기체 비행 검증을 대신하는 시험은 아니다.

"""Direct, local-only transport for Bambu Lab P1-series printers.

VANIOR PRINT never imports or launches Bambu Studio here.  A prepared
``.gcode.3mf`` is uploaded to the printer over implicit FTPS and the print job
is started over MQTT/TLS.  Both TLS endpoints are pinned using trust on first
use (TOFU); production dispatch refuses an unpinned printer.
"""

from __future__ import annotations

import ftplib
import hashlib
import ipaddress
import json
import logging
import re
import socket
import ssl
import time
import uuid
import zipfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import Event
from typing import Any

import paho.mqtt.client as mqtt
from paho.mqtt import MQTTException

from .input_safety import file_identity
from .vanior_3mf import GCODE_PATH, SETTINGS_PATH, verify_vanior_gcode_3mf

MQTT_PORT = 8883
FTPS_PORT = 990
MQTT_USERNAME = "bblp"
_SERIAL_PATTERN = re.compile(r"^[A-Za-z0-9]{8,32}$")
_ACCESS_CODE_PATTERN = re.compile(r"^[A-Za-z0-9]{8,32}$")
_ACTIVE_STATES = {
    "RUNNING",
    "PREPARE",
    "PAUSE",
    "SLICING",
    "INIT",
}
_READY_STATES = {"IDLE", "FINISH"}
_MAX_PACKAGE_BYTES = 640 * 1024 * 1024
LOGGER = logging.getLogger(__name__)


class PrinterTransportError(RuntimeError):
    """A direct printer connection, validation, upload, or start failed."""


@dataclass(frozen=True)
class PrinterConnectionConfig:
    host: str
    serial: str
    access_code: str
    mqtt_certificate_sha256: str = ""
    ftps_certificate_sha256: str = ""
    timeout_s: float = 12.0

    def validated(self, *, require_trust: bool = False) -> PrinterConnectionConfig:
        try:
            address = ipaddress.ip_address(self.host.strip())
        except ValueError as exc:
            raise PrinterTransportError("Укажите IP-адрес принтера в локальной сети.") from exc
        if (
            address.version != 4
            or not (address.is_private or address.is_link_local)
            or address.is_loopback
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        ):
            raise PrinterTransportError(
                "VANIOR PRINT разрешает отправку только на локальный IPv4-адрес."
            )
        serial = self.serial.strip().upper()
        if not _SERIAL_PATTERN.fullmatch(serial):
            raise PrinterTransportError("Серийный номер принтера имеет неверный формат.")
        access_code = self.access_code.strip()
        if not _ACCESS_CODE_PATTERN.fullmatch(access_code):
            raise PrinterTransportError(
                "Код доступа должен содержать 8–32 латинских буквы или цифры."
            )
        mqtt_fingerprint = _normalize_fingerprint(self.mqtt_certificate_sha256)
        ftps_fingerprint = _normalize_fingerprint(self.ftps_certificate_sha256)
        if require_trust and (not mqtt_fingerprint or not ftps_fingerprint):
            raise PrinterTransportError(
                "Сначала нажмите «Проверить и доверять принтеру»."
            )
        return PrinterConnectionConfig(
            host=str(address),
            serial=serial,
            access_code=access_code,
            mqtt_certificate_sha256=mqtt_fingerprint,
            ftps_certificate_sha256=ftps_fingerprint,
            timeout_s=max(3.0, min(float(self.timeout_s), 60.0)),
        )


@dataclass(frozen=True)
class PrinterStatus:
    state: str
    current_job: str
    progress_percent: int | None
    nozzle_diameter_mm: float | None
    ams_present: bool
    raw: dict[str, Any]

    @property
    def busy(self) -> bool:
        return self.state.upper() in _ACTIVE_STATES

    @property
    def ready_for_new_job(self) -> bool:
        return self.state.upper() in _READY_STATES


@dataclass(frozen=True)
class PrinterConnectionTest:
    host: str
    serial: str
    mqtt_certificate_sha256: str
    ftps_certificate_sha256: str
    status: PrinterStatus

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"].pop("raw", None)
        return payload


@dataclass(frozen=True)
class PrintDispatchOptions:
    use_ams: bool = False
    ams_slot: int = 1
    bed_leveling: bool = True
    vibration_calibration: bool = True
    flow_calibration: bool = False
    timelapse: bool = False
    expected_material: str = ""

    def validated(self) -> PrintDispatchOptions:
        if not 1 <= int(self.ams_slot) <= 4:
            raise PrinterTransportError("Номер слота AMS должен быть от 1 до 4.")
        material = self.expected_material.strip().upper()
        if material and material not in {"PLA", "PETG"}:
            raise PrinterTransportError("Для прямой печати поддерживаются только PLA и PETG.")
        return PrintDispatchOptions(
            use_ams=bool(self.use_ams),
            ams_slot=int(self.ams_slot),
            bed_leveling=bool(self.bed_leveling),
            vibration_calibration=bool(self.vibration_calibration),
            flow_calibration=bool(self.flow_calibration),
            timelapse=bool(self.timelapse),
            expected_material=material,
        )


@dataclass(frozen=True)
class PrintDispatchResult:
    local_file: Path
    remote_file: str
    uploaded_bytes: int
    package_sha256: str
    confirmed: bool
    printer_state: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["local_file"] = str(self.local_file)
        return payload


def _normalize_fingerprint(value: str) -> str:
    normalized = re.sub(r"[^0-9A-Fa-f]", "", str(value)).upper()
    if normalized and len(normalized) != 64:
        raise PrinterTransportError("Отпечаток сертификата должен быть SHA-256.")
    return normalized


def _mqtt_reason_value(reason_code: Any) -> int:
    """Return a numeric MQTT reason code across Paho callback API versions.

    Paho MQTT 2.x passes ``ReasonCode`` objects to VERSION2 callbacks.  Those
    objects intentionally do not implement ``int()``; their numeric code lives
    in ``.value``.  An exception raised by a callback runs on Paho's network
    thread and otherwise looks to the caller like a connection timeout.
    """
    value = getattr(reason_code, "value", reason_code)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 255


def _unverified_tls_context() -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def certificate_sha256(host: str, port: int, timeout_s: float) -> str:
    """Return the peer certificate fingerprint for explicit user trust."""
    context = _unverified_tls_context()
    try:
        with (
            socket.create_connection((host, port), timeout=timeout_s) as raw,
            context.wrap_socket(raw, server_hostname=host) as secured,
        ):
            certificate = secured.getpeercert(binary_form=True)
    except (OSError, ssl.SSLError) as exc:
        raise PrinterTransportError(
            f"Принтер {host}:{port} недоступен по TLS: {exc}"
        ) from exc
    if not certificate:
        raise PrinterTransportError(f"Принтер {host}:{port} не предъявил сертификат.")
    return hashlib.sha256(certificate).hexdigest().upper()


def _verify_pinned_endpoint(
    host: str, port: int, expected_fingerprint: str, timeout_s: float
) -> str:
    actual = certificate_sha256(host, port, timeout_s)
    expected = _normalize_fingerprint(expected_fingerprint)
    if expected and actual != expected:
        raise PrinterTransportError(
            f"Сертификат принтера на порту {port} изменился. Отправка заблокирована."
        )
    return actual


class _ImplicitFTP_TLS(ftplib.FTP_TLS):
    """``FTP_TLS`` variant that performs TLS before the FTP greeting."""

    def connect(
        self,
        host: str = "",
        port: int = 0,
        timeout: float | None = None,
        source_address: tuple[str, int] | None = None,
    ) -> str:
        if host:
            self.host = host
        if port:
            self.port = port
        if timeout is not None:
            self.timeout = timeout
        self.sock = socket.create_connection(
            (self.host, self.port), self.timeout, source_address=source_address
        )
        self.af = self.sock.family
        self.sock = self.context.wrap_socket(self.sock, server_hostname=self.host)
        self.file = self.sock.makefile("r", encoding=self.encoding)
        self.welcome = self.getresp()
        return self.welcome

    def auth(self) -> str:
        # The control connection is already protected by implicit TLS.
        return "234 Already using TLS."


def _parse_printer_status(payload: dict[str, Any]) -> PrinterStatus:
    print_data = payload.get("print")
    if not isinstance(print_data, dict):
        print_data = payload
    state = str(print_data.get("gcode_state") or print_data.get("state") or "UNKNOWN")
    current_job = str(
        print_data.get("subtask_name")
        or print_data.get("gcode_file")
        or print_data.get("file")
        or ""
    )
    progress_raw = print_data.get("mc_percent")
    try:
        progress = max(0, min(100, int(progress_raw))) if progress_raw is not None else None
    except (TypeError, ValueError):
        progress = None
    nozzle_raw = print_data.get("nozzle_diameter")
    try:
        nozzle = float(nozzle_raw) if nozzle_raw not in (None, "") else None
    except (TypeError, ValueError):
        nozzle = None
    ams = print_data.get("ams")
    ams_present = bool(ams and isinstance(ams, dict) and ams.get("ams"))
    return PrinterStatus(
        state=state.upper(),
        current_job=current_job,
        progress_percent=progress,
        nozzle_diameter_mm=nozzle,
        ams_present=ams_present,
        raw=payload,
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest().upper()


def _job_remote_name(package_sha256: str) -> str:
    unique = uuid.uuid4().hex[:6]
    return (
        f"vanior-{time.strftime('%Y%m%d-%H%M%S')}-"
        f"{package_sha256[:12].lower()}-{unique}.gcode.3mf"
    )


def _inspect_print_package(package: Path) -> tuple[str, str]:
    verification = verify_vanior_gcode_3mf(package)
    if not verification.valid:
        raise PrinterTransportError(
            "Печатный 3MF не прошёл проверку VANIOR: " + "; ".join(verification.errors)
        )
    try:
        with zipfile.ZipFile(package) as archive:
            settings = json.loads(archive.read(SETTINGS_PATH))
            if GCODE_PATH not in archive.namelist():
                raise PrinterTransportError("В печатном 3MF отсутствует plate_1.gcode.")
    except (OSError, KeyError, ValueError, zipfile.BadZipFile) as exc:
        raise PrinterTransportError(f"Не удалось проверить печатный 3MF: {exc}") from exc
    printer = str(settings.get("printer", ""))
    if "P1S" not in printer.upper():
        raise PrinterTransportError("Задание не предназначено для Bambu Lab P1S.")
    nozzle = (settings.get("settings") or {}).get("nozzle_diameter_mm", 0.4)
    try:
        if abs(float(nozzle) - 0.4) > 1e-6:
            raise PrinterTransportError("Прямая печать сейчас проверена только для сопла 0,4 мм.")
    except (TypeError, ValueError) as exc:
        raise PrinterTransportError("В задании отсутствует диаметр сопла.") from exc
    return verification.gcode_sha256, str(settings.get("material", "UNKNOWN"))


def _build_print_command(
    remote_file: str,
    sequence_id: str,
    options: PrintDispatchOptions,
) -> dict[str, Any]:
    mapping = [options.ams_slot - 1] if options.use_ams else [-1]
    return {
        "print": {
            "sequence_id": sequence_id,
            "command": "project_file",
            "param": GCODE_PATH,
            "subtask_name": remote_file,
            "file": remote_file,
            "url": f"ftp:///{remote_file}",
            "md5": "",
            "project_id": "0",
            "profile_id": "0",
            "task_id": "0",
            "subtask_id": "0",
            "timelapse": options.timelapse,
            "bed_type": "auto",
            "bed_leveling": options.bed_leveling,
            "bed_levelling": options.bed_leveling,
            "flow_cali": options.flow_calibration,
            "vibration_cali": options.vibration_calibration,
            "layer_inspect": False,
            "use_ams": options.use_ams,
            "ams_mapping": mapping,
        }
    }


class LocalP1SClient:
    """Upload and start a verified VANIOR job without any Bambu application."""

    def __init__(
        self,
        config: PrinterConnectionConfig,
        *,
        progress_callback: Callable[[str, int], None] | None = None,
    ) -> None:
        self.config = config
        self.progress_callback = progress_callback

    def _progress(self, message: str, percent: int) -> None:
        if self.progress_callback:
            self.progress_callback(message, max(0, min(100, int(percent))))

    def _mqtt_exchange(
        self,
        config: PrinterConnectionConfig,
        request: dict[str, Any],
        predicate: Callable[[dict[str, Any]], bool],
        *,
        response_timeout_s: float,
    ) -> dict[str, Any] | None:
        connected = Event()
        subscribed = Event()
        received = Event()
        result: dict[str, Any] = {}
        failure: list[str] = []

        def on_connect(
            client: mqtt.Client,
            _userdata: Any,
            _flags: Any,
            reason_code: Any,
            _properties: Any,
        ) -> None:
            try:
                if _mqtt_reason_value(reason_code) != 0:
                    failure.append(f"MQTT отклонён, код {reason_code}")
                    return
                client.subscribe(f"device/{config.serial}/report", qos=1)
            except Exception as exc:  # noqa: BLE001 -- callback thread boundary
                failure.append(f"Ошибка обработчика MQTT-подключения: {exc}")
                subscribed.set()
            finally:
                connected.set()

        def on_subscribe(
            _client: mqtt.Client,
            _userdata: Any,
            _mid: int,
            reason_codes: list[Any],
            _properties: Any,
        ) -> None:
            try:
                if any(_mqtt_reason_value(code) >= 128 for code in reason_codes):
                    failure.append("Принтер отклонил подписку на канал состояния.")
            except Exception as exc:  # noqa: BLE001 -- callback thread boundary
                failure.append(f"Ошибка подтверждения MQTT-подписки: {exc}")
            finally:
                subscribed.set()

        def on_message(_client: mqtt.Client, _userdata: Any, message: mqtt.MQTTMessage) -> None:
            try:
                payload = json.loads(message.payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return
            if isinstance(payload, dict) and predicate(payload):
                result.update(payload)
                received.set()

        def on_disconnect(
            _client: mqtt.Client,
            _userdata: Any,
            _disconnect_flags: Any,
            reason_code: Any,
            _properties: Any,
        ) -> None:
            code = _mqtt_reason_value(reason_code)
            if code != 0:
                failure.append(f"MQTT-соединение потеряно, код {reason_code}")
                received.set()

        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"vanior-{uuid.uuid4().hex[:12]}",
            protocol=mqtt.MQTTv311,
            clean_session=True,
        )
        client.username_pw_set(MQTT_USERNAME, config.access_code)
        client.tls_set_context(_unverified_tls_context())
        client.on_connect = on_connect
        client.on_subscribe = on_subscribe
        client.on_message = on_message
        client.on_disconnect = on_disconnect
        try:
            client.connect(config.host, MQTT_PORT, keepalive=30)
            client.loop_start()
            if not connected.wait(config.timeout_s):
                raise PrinterTransportError(
                    "Принтер не ответил на MQTT-подключение. Проверьте IP, LAN Only/Developer Mode, "
                    "код доступа и отсутствие изоляции устройств в Wi-Fi."
                )
            if failure:
                raise PrinterTransportError(failure[0])
            if not subscribed.wait(config.timeout_s):
                raise PrinterTransportError("Принтер не подтвердил подписку на канал состояния.")
            if failure:
                raise PrinterTransportError(failure[0])
            info = client.publish(
                f"device/{config.serial}/request",
                json.dumps(request, separators=(",", ":"), ensure_ascii=True),
                # P1-series LAN brokers can accept the command but omit PUBACK
                # for QoS 1.  The widely used local client path therefore uses
                # QoS 0 and verifies the command through the report topic.
                qos=0,
            )
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                raise PrinterTransportError(
                    f"MQTT-клиент не поставил команду в очередь, код {info.rc}."
                )
            received.wait(response_timeout_s)
            if failure:
                raise PrinterTransportError(failure[0])
            return dict(result) if result else None
        except (OSError, ssl.SSLError, MQTTException) as exc:
            raise PrinterTransportError(f"Ошибка MQTT-соединения: {exc}") from exc
        finally:
            try:
                client.disconnect()
                client.loop_stop()
            except (OSError, MQTTException) as cleanup_error:
                LOGGER.debug("MQTT cleanup failed: %s", cleanup_error)

    def _request_status(self, config: PrinterConnectionConfig) -> PrinterStatus:
        sequence_id = str(int(time.time() * 1000))
        response = self._mqtt_exchange(
            config,
            {"pushing": {"sequence_id": sequence_id, "command": "pushall"}},
            lambda payload: isinstance(payload.get("print"), dict)
            and bool(payload["print"].get("gcode_state")),
            response_timeout_s=config.timeout_s,
        )
        if response is None:
            raise PrinterTransportError("Принтер не передал своё состояние.")
        return _parse_printer_status(response)

    def _ftps_session(self, config: PrinterConnectionConfig) -> _ImplicitFTP_TLS:
        session = _ImplicitFTP_TLS(context=_unverified_tls_context(), timeout=config.timeout_s)
        try:
            session.connect(config.host, FTPS_PORT, timeout=config.timeout_s)
            certificate = session.sock.getpeercert(binary_form=True) if session.sock else None
            if not certificate:
                raise PrinterTransportError("FTPS-канал не предъявил сертификат принтера.")
            actual_fingerprint = hashlib.sha256(certificate).hexdigest().upper()
            if actual_fingerprint != _normalize_fingerprint(
                config.ftps_certificate_sha256
            ):
                raise PrinterTransportError(
                    "Сертификат FTPS изменился во время подключения; доступ заблокирован."
                )
            session.login(MQTT_USERNAME, config.access_code)
            session.prot_p()
            session.set_pasv(True)
            return session
        except PrinterTransportError:
            try:
                session.close()
            except OSError as cleanup_error:
                LOGGER.debug("FTPS cleanup failed: %s", cleanup_error)
            raise
        except ftplib.all_errors as exc:
            try:
                session.close()
            except OSError as cleanup_error:
                LOGGER.debug("FTPS cleanup failed: %s", cleanup_error)
            raise PrinterTransportError(f"Ошибка защищённого файлового канала: {exc}") from exc

    def test_connection(self) -> PrinterConnectionTest:
        config = self.config.validated(require_trust=False)
        self._progress("Проверка сертификата управляющего канала", 15)
        mqtt_fingerprint = _verify_pinned_endpoint(
            config.host, MQTT_PORT, config.mqtt_certificate_sha256, config.timeout_s
        )
        self._progress("Проверка сертификата канала загрузки", 35)
        ftps_fingerprint = _verify_pinned_endpoint(
            config.host, FTPS_PORT, config.ftps_certificate_sha256, config.timeout_s
        )
        if not config.mqtt_certificate_sha256 or not config.ftps_certificate_sha256:
            # First contact only reads the public certificates.  The access code is
            # not sent until the user has explicitly pinned both endpoints.
            return PrinterConnectionTest(
                host=config.host,
                serial=config.serial,
                mqtt_certificate_sha256=mqtt_fingerprint,
                ftps_certificate_sha256=ftps_fingerprint,
                status=PrinterStatus("UNVERIFIED", "", None, None, False, {}),
            )
        trusted = PrinterConnectionConfig(
            host=config.host,
            serial=config.serial,
            access_code=config.access_code,
            mqtt_certificate_sha256=mqtt_fingerprint,
            ftps_certificate_sha256=ftps_fingerprint,
            timeout_s=config.timeout_s,
        )
        self._progress("Проверка кода доступа", 55)
        session = self._ftps_session(trusted)
        try:
            session.pwd()
        finally:
            try:
                session.quit()
            except ftplib.all_errors:
                session.close()
        self._progress("Получение состояния P1S", 75)
        status = self._request_status(trusted)
        self._progress("Принтер проверен", 100)
        return PrinterConnectionTest(
            host=config.host,
            serial=config.serial,
            mqtt_certificate_sha256=mqtt_fingerprint,
            ftps_certificate_sha256=ftps_fingerprint,
            status=status,
        )

    def send_print(
        self,
        package: str | Path,
        options: PrintDispatchOptions,
    ) -> PrintDispatchResult:
        config = self.config.validated(require_trust=True)
        options = options.validated()
        package_path = Path(package).expanduser().resolve()
        if not package_path.is_file():
            raise PrinterTransportError("Печатный 3MF не найден.")
        package_size = package_path.stat().st_size
        if package_size <= 0 or package_size > _MAX_PACKAGE_BYTES:
            raise PrinterTransportError(
                "Размер печатного 3MF недопустим для безопасной отправки."
            )
        self._progress("Проверка задания VANIOR", 5)
        initial_identity = file_identity(package_path)
        _gcode_hash, package_material = _inspect_print_package(package_path)
        if options.expected_material and package_material.upper() != options.expected_material:
            raise PrinterTransportError(
                f"В задании указан {package_material}, а в интерфейсе выбран "
                f"{options.expected_material}. Выполните оптимизацию повторно."
            )
        package_hash = _sha256_file(package_path)
        if file_identity(package_path) != initial_identity:
            raise PrinterTransportError(
                "Печатный 3MF изменился во время проверки; отправка заблокирована."
            )
        self._progress("Проверка закреплённых сертификатов принтера", 12)
        _verify_pinned_endpoint(
            config.host, MQTT_PORT, config.mqtt_certificate_sha256, config.timeout_s
        )
        _verify_pinned_endpoint(
            config.host, FTPS_PORT, config.ftps_certificate_sha256, config.timeout_s
        )
        self._progress("Проверка состояния принтера", 18)
        status = self._request_status(config)
        if status.busy:
            raise PrinterTransportError(
                f"Принтер занят ({status.state}); текущее задание: {status.current_job or 'без имени'}."
            )
        if not status.ready_for_new_job:
            raise PrinterTransportError(
                f"Принтер сообщил состояние {status.state}; безопасный запуск не подтверждён."
            )
        if status.nozzle_diameter_mm is None:
            raise PrinterTransportError(
                "Принтер не сообщил диаметр сопла; отправка заблокирована."
            )
        if abs(status.nozzle_diameter_mm - 0.4) > 0.01:
            raise PrinterTransportError(
                f"На принтере установлено сопло {status.nozzle_diameter_mm:g} мм, а задание рассчитано для 0,4 мм."
            )
        if options.use_ams and not status.ams_present:
            raise PrinterTransportError(
                "Выбран слот AMS, но принтер не подтвердил подключённый AMS."
            )
        remote_file = _job_remote_name(package_hash)
        self._progress("Загрузка печатного 3MF на принтер", 25)
        session = self._ftps_session(config)
        uploaded = 0

        def count_block(block: bytes) -> None:
            nonlocal uploaded
            uploaded += len(block)
            self._progress(
                "Загрузка печатного 3MF на принтер",
                25 + int(55 * uploaded / package_size),
            )

        try:
            with package_path.open("rb") as stream:
                session.storbinary(
                    f"STOR {remote_file}", stream, blocksize=128 * 1024, callback=count_block
                )
            remote_size = session.size(remote_file)
            if file_identity(package_path) != initial_identity:
                try:
                    session.delete(remote_file)
                except ftplib.all_errors as cleanup_error:
                    LOGGER.debug("Could not delete changed remote job: %s", cleanup_error)
                raise PrinterTransportError(
                    "Печатный 3MF изменился во время загрузки; удалённый файл отклонён."
                )
            if remote_size is None or int(remote_size) != package_size:
                try:
                    session.delete(remote_file)
                except ftplib.all_errors as cleanup_error:
                    LOGGER.debug("Could not delete incomplete remote job: %s", cleanup_error)
                raise PrinterTransportError(
                    "Принтер не подтвердил точный размер загруженного файла; печать отменена."
                )
        except ftplib.all_errors as exc:
            raise PrinterTransportError(f"Не удалось загрузить задание: {exc}") from exc
        finally:
            try:
                session.quit()
            except ftplib.all_errors:
                session.close()
        self._progress("Отправка команды запуска", 85)
        sequence_id = str(int(time.time() * 1000))
        command = _build_print_command(remote_file, sequence_id, options)

        def is_confirmation(payload: dict[str, Any]) -> bool:
            print_data = payload.get("print")
            if not isinstance(print_data, dict):
                return False
            if str(print_data.get("sequence_id", "")) == sequence_id:
                return True
            reported_file = str(
                print_data.get("subtask_name")
                or print_data.get("gcode_file")
                or print_data.get("file")
                or ""
            )
            return (
                reported_file == remote_file
                and str(print_data.get("gcode_state", "")).upper() in _ACTIVE_STATES
            )

        response = self._mqtt_exchange(
            config, command, is_confirmation, response_timeout_s=20.0
        )
        confirmed = response is not None
        printer_state = "UNKNOWN"
        if response:
            print_data = response.get("print") or {}
            result = str(print_data.get("result", "success")).lower()
            reason = str(print_data.get("reason", ""))
            if result not in {"", "success", "0"}:
                raise PrinterTransportError(
                    "Принтер отклонил запуск" + (f": {reason}" if reason else ".")
                )
            printer_state = _parse_printer_status(response).state
        self._progress("Задание передано принтеру", 100)
        message = (
            "Принтер подтвердил запуск задания."
            if confirmed
            else "Команда передана, но подтверждение запуска не получено. Проверьте экран принтера перед повторной отправкой."
        )
        return PrintDispatchResult(
            local_file=package_path,
            remote_file=remote_file,
            uploaded_bytes=uploaded,
            package_sha256=package_hash,
            confirmed=confirmed,
            printer_state=printer_state,
            message=message,
        )


__all__ = [
    "FTPS_PORT",
    "MQTT_PORT",
    "LocalP1SClient",
    "PrintDispatchOptions",
    "PrintDispatchResult",
    "PrinterConnectionConfig",
    "PrinterConnectionTest",
    "PrinterStatus",
    "PrinterTransportError",
]

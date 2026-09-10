from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

from ai_print_optimizer.printer_transport import (
    LocalP1SClient,
    PrintDispatchOptions,
    PrinterConnectionConfig,
    PrinterStatus,
    PrinterTransportError,
    _build_print_command,
    _inspect_print_package,
    _parse_printer_status,
)
from ai_print_optimizer.vanior_3mf import (
    GCODE_PATH,
    SETTINGS_PATH,
    Vanior3MFVerification,
)


class PrinterTransportTests(unittest.TestCase):
    def test_configuration_allows_only_local_ipv4(self) -> None:
        valid = PrinterConnectionConfig(
            "192.168.1.44", "01P00A123456789", "A1B2C3D4"
        ).validated()
        self.assertEqual(valid.host, "192.168.1.44")

        with self.assertRaisesRegex(PrinterTransportError, "локальный"):
            PrinterConnectionConfig(
                "8.8.8.8", "01P00A123456789", "A1B2C3D4"
            ).validated()
        for unsafe_host in ("127.0.0.1", "0.0.0.0", "224.0.0.1"):
            with self.subTest(host=unsafe_host), self.assertRaisesRegex(
                PrinterTransportError, "локальный"
            ):
                PrinterConnectionConfig(
                    unsafe_host, "01P00A123456789", "A1B2C3D4"
                ).validated()

    def test_production_dispatch_requires_both_pinned_certificates(self) -> None:
        with self.assertRaisesRegex(PrinterTransportError, "доверять"):
            PrinterConnectionConfig(
                "192.168.1.44", "01P00A123456789", "A1B2C3D4"
            ).validated(require_trust=True)

    def test_print_command_maps_external_spool_and_ams_slots(self) -> None:
        external = _build_print_command(
            "job.gcode.3mf", "41", PrintDispatchOptions(use_ams=False)
        )["print"]
        ams = _build_print_command(
            "job.gcode.3mf", "42", PrintDispatchOptions(use_ams=True, ams_slot=4)
        )["print"]

        self.assertEqual(external["url"], "ftp:///job.gcode.3mf")
        self.assertEqual(external["param"], GCODE_PATH)
        self.assertFalse(external["use_ams"])
        self.assertEqual(external["ams_mapping"], [-1])
        self.assertTrue(ams["use_ams"])
        self.assertEqual(ams["ams_mapping"], [3])

    def test_status_parser_reports_busy_print_and_nozzle(self) -> None:
        status = _parse_printer_status(
            {
                "print": {
                    "gcode_state": "RUNNING",
                    "subtask_name": "cube",
                    "mc_percent": "63",
                    "nozzle_diameter": "0.4",
                    "ams": {"ams": [{"id": "0"}]},
                }
            }
        )

        self.assertTrue(status.busy)
        self.assertEqual(status.progress_percent, 63)
        self.assertEqual(status.nozzle_diameter_mm, 0.4)
        self.assertTrue(status.ams_present)

    def test_first_connection_only_reads_certificates_before_user_trust(self) -> None:
        config = PrinterConnectionConfig(
            "192.168.1.44", "01P00A123456789", "A1B2C3D4"
        )
        client = LocalP1SClient(config)
        with (
            mock.patch(
                "ai_print_optimizer.printer_transport._verify_pinned_endpoint",
                side_effect=["A" * 64, "B" * 64],
            ),
            mock.patch.object(client, "_ftps_session") as ftps,
            mock.patch.object(client, "_request_status") as status,
        ):
            result = client.test_connection()

        self.assertEqual(result.status.state, "UNVERIFIED")
        self.assertEqual(result.mqtt_certificate_sha256, "A" * 64)
        self.assertEqual(result.ftps_certificate_sha256, "B" * 64)
        ftps.assert_not_called()
        status.assert_not_called()

    def test_mqtt_socket_failure_is_reported_without_attribute_error(self) -> None:
        config = PrinterConnectionConfig(
            "192.168.1.44", "01P00A123456789", "A1B2C3D4"
        )
        client = LocalP1SClient(config)
        mqtt_client = mock.MagicMock()
        mqtt_client.connect.side_effect = OSError("connection refused")
        with (
            mock.patch(
                "ai_print_optimizer.printer_transport.mqtt.Client",
                return_value=mqtt_client,
            ),
            self.assertRaisesRegex(PrinterTransportError, "Ошибка MQTT-соединения"),
        ):
            client._mqtt_exchange(
                config,
                {"pushing": {"command": "pushall"}},
                lambda _payload: True,
                response_timeout_s=0.01,
            )

    def test_mqtt_v2_reason_codes_complete_connection_and_subscription(self) -> None:
        config = PrinterConnectionConfig(
            "192.168.1.44", "01P00A123456789", "A1B2C3D4"
        )
        client = LocalP1SClient(config)

        class PublishInfo:
            rc = 0

            def wait_for_publish(self, timeout=None):
                return None

            def is_published(self):
                return True

        class Message:
            payload = json.dumps(
                {"print": {"gcode_state": "IDLE", "sequence_id": "1"}}
            ).encode("utf-8")

        class FakeMQTT:
            on_connect = None
            on_subscribe = None
            on_message = None
            on_disconnect = None
            published_qos = None

            def username_pw_set(self, _username, _password):
                return None

            def tls_set_context(self, _context):
                return None

            def connect(self, _host, _port, keepalive=30):
                return 0

            def loop_start(self):
                self.on_connect(
                    self,
                    None,
                    {},
                    ReasonCode(PacketTypes.CONNACK, identifier=0),
                    None,
                )

            def subscribe(self, _topic, qos=0):
                self.on_subscribe(
                    self,
                    None,
                    1,
                    [ReasonCode(PacketTypes.SUBACK, identifier=qos)],
                    None,
                )
                return 0, 1

            def publish(self, _topic, _payload, qos=0):
                self.published_qos = qos
                self.on_message(self, None, Message())
                return PublishInfo()

            def disconnect(self):
                return 0

            def loop_stop(self):
                return None

        fake_mqtt = FakeMQTT()
        with mock.patch(
            "ai_print_optimizer.printer_transport.mqtt.Client",
            return_value=fake_mqtt,
        ):
            response = client._mqtt_exchange(
                config,
                {"pushing": {"command": "pushall"}},
                lambda payload: payload.get("print", {}).get("gcode_state") == "IDLE",
                response_timeout_s=0.01,
            )

        self.assertEqual(response["print"]["gcode_state"], "IDLE")
        self.assertEqual(fake_mqtt.published_qos, 0)

    def test_package_inspection_requires_verified_p1s_job(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / "job.gcode.3mf"
            with zipfile.ZipFile(package, "w") as archive:
                archive.writestr(GCODE_PATH, "; generated by VANIOR Slice\n")
                archive.writestr(
                    SETTINGS_PATH,
                    json.dumps(
                        {
                            "printer": "Bambu Lab P1S (профиль VANIOR)",
                            "material": "PLA",
                            "settings": {},
                        }
                    ),
                )
            verification = Vanior3MFVerification(
                valid=True,
                path=package,
                errors=(),
                entries=(GCODE_PATH, SETTINGS_PATH),
                vertex_count=3,
                triangle_count=1,
                gcode_sha256="A" * 64,
            )
            with mock.patch(
                "ai_print_optimizer.printer_transport.verify_vanior_gcode_3mf",
                return_value=verification,
            ):
                gcode_hash, material = _inspect_print_package(package)

        self.assertEqual(gcode_hash, "A" * 64)
        self.assertEqual(material, "PLA")

    def test_send_refuses_busy_printer_before_upload(self) -> None:
        config = PrinterConnectionConfig(
            "192.168.1.44",
            "01P00A123456789",
            "A1B2C3D4",
            "A" * 64,
            "B" * 64,
        )
        client = LocalP1SClient(config)
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / "job.gcode.3mf"
            package.write_bytes(b"verified package")
            busy = PrinterStatus("RUNNING", "existing-job", 10, 0.4, False, {})
            with (
                mock.patch(
                    "ai_print_optimizer.printer_transport._verify_pinned_endpoint"
                ),
                mock.patch(
                    "ai_print_optimizer.printer_transport._inspect_print_package",
                    return_value=(hashlib.sha256(b"gcode").hexdigest(), "PLA"),
                ),
                mock.patch.object(client, "_request_status", return_value=busy),
                mock.patch.object(client, "_ftps_session") as session,
            ):
                with self.assertRaisesRegex(PrinterTransportError, "занят"):
                    client.send_print(package, PrintDispatchOptions())
                session.assert_not_called()

    def test_send_requires_safe_idle_state_nozzle_and_requested_ams(self) -> None:
        config = PrinterConnectionConfig(
            "192.168.1.44",
            "01P00A123456789",
            "A1B2C3D4",
            "A" * 64,
            "B" * 64,
        )
        cases = (
            (PrinterStatus("UNKNOWN", "", None, 0.4, True, {}), PrintDispatchOptions(), "безопасный запуск"),
            (PrinterStatus("IDLE", "", None, None, True, {}), PrintDispatchOptions(), "диаметр сопла"),
            (PrinterStatus("IDLE", "", None, 0.4, False, {}), PrintDispatchOptions(use_ams=True), "AMS"),
        )
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / "job.gcode.3mf"
            package.write_bytes(b"verified package")
            for status, options, message in cases:
                with self.subTest(message=message):
                    client = LocalP1SClient(config)
                    with (
                        mock.patch(
                            "ai_print_optimizer.printer_transport._verify_pinned_endpoint"
                        ),
                        mock.patch(
                            "ai_print_optimizer.printer_transport._inspect_print_package",
                            return_value=(hashlib.sha256(b"gcode").hexdigest(), "PLA"),
                        ),
                        mock.patch.object(client, "_request_status", return_value=status),
                        mock.patch.object(client, "_ftps_session") as session,
                        self.assertRaisesRegex(PrinterTransportError, message),
                    ):
                        client.send_print(package, options)
                    session.assert_not_called()

    def test_send_refuses_material_changed_after_slicing(self) -> None:
        config = PrinterConnectionConfig(
            "192.168.1.44",
            "01P00A123456789",
            "A1B2C3D4",
            "A" * 64,
            "B" * 64,
        )
        client = LocalP1SClient(config)
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / "job.gcode.3mf"
            package.write_bytes(b"verified package")
            with (
                mock.patch(
                    "ai_print_optimizer.printer_transport._inspect_print_package",
                    return_value=(hashlib.sha256(b"gcode").hexdigest(), "PLA"),
                ),
                mock.patch.object(client, "_request_status") as status,
                self.assertRaisesRegex(PrinterTransportError, "интерфейсе выбран PETG"),
            ):
                client.send_print(
                    package,
                    PrintDispatchOptions(expected_material="PETG"),
                )
            status.assert_not_called()

    def test_send_uploads_verified_package_then_starts_job(self) -> None:
        config = PrinterConnectionConfig(
            "192.168.1.44",
            "01P00A123456789",
            "A1B2C3D4",
            "A" * 64,
            "B" * 64,
        )
        progress: list[tuple[str, int]] = []
        client = LocalP1SClient(
            config, progress_callback=lambda message, percent: progress.append((message, percent))
        )

        class FakeFTPS:
            uploaded = b""
            remote = ""

            def storbinary(self, command, stream, blocksize=8192, callback=None):
                self.remote = command.removeprefix("STOR ")
                chunks = []
                while block := stream.read(blocksize):
                    chunks.append(block)
                    if callback:
                        callback(block)
                self.uploaded = b"".join(chunks)

            def size(self, _name):
                return len(self.uploaded)

            def quit(self):
                return None

            def close(self):
                return None

        fake_ftps = FakeFTPS()
        idle = PrinterStatus("IDLE", "", 0, 0.4, True, {})
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / "job.gcode.3mf"
            package.write_bytes(b"verified package")
            with (
                mock.patch(
                    "ai_print_optimizer.printer_transport._verify_pinned_endpoint"
                ),
                mock.patch(
                    "ai_print_optimizer.printer_transport._inspect_print_package",
                    return_value=(hashlib.sha256(b"gcode").hexdigest(), "PLA"),
                ),
                mock.patch.object(client, "_request_status", return_value=idle),
                mock.patch.object(client, "_ftps_session", return_value=fake_ftps),
                mock.patch.object(
                    client,
                    "_mqtt_exchange",
                    return_value={
                        "print": {
                            "sequence_id": "accepted",
                            "result": "success",
                            "gcode_state": "PREPARE",
                        }
                    },
                ) as exchange,
            ):
                result = client.send_print(
                    package,
                    PrintDispatchOptions(use_ams=True, ams_slot=2),
                )

        self.assertTrue(result.confirmed)
        self.assertEqual(result.printer_state, "PREPARE")
        self.assertEqual(fake_ftps.uploaded, b"verified package")
        self.assertTrue(fake_ftps.remote.endswith(".gcode.3mf"))
        command = exchange.call_args.args[1]["print"]
        self.assertEqual(command["ams_mapping"], [1])
        self.assertEqual(command["file"], fake_ftps.remote)
        self.assertEqual(progress[-1][1], 100)


if __name__ == "__main__":
    unittest.main()

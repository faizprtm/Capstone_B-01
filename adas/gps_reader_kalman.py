#!/usr/bin/env python3
"""
gps_reader.py — Threaded GPS NMEA reader with a 1D Kalman Filter.

Kompatibel dengan interface GPSReader yang digunakan oleh adas/pipeline.py:
    GPSReader()
    start()
    stop()
    ego_speed_ms
    gps_status
    is_valid

Data tambahan untuk evaluasi:
    raw_speed_ms
    kalman_gain

Catatan:
    ego_speed_ms bernilai 0.0 ketika GPS tidak valid untuk kompatibilitas.
    Nilai tersebut BUKAN bukti kendaraan sedang berhenti. Pipeline wajib
    memeriksa is_valid sebelum menggunakan kecepatan untuk perhitungan TTC.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from typing import Optional

import serial

from .constants import GPS_PORT, GPS_BAUD, GPS_STALE_TIMEOUT_S

log = logging.getLogger(__name__)

KNOTS_TO_MS = 0.5144444

# Parameter awal untuk pengujian, belum merupakan hasil kalibrasi bus.
# P0 dan R berdimensi (m/s)^2; Q berdimensi (m/s)^2 per detik.
KALMAN_INITIAL_P = 1.0
KALMAN_Q = 0.5
KALMAN_R = 2.0


class KalmanSpeedFilter:
    """Kalman Filter 1D untuk estimasi kecepatan GPS (m/s)."""

    def __init__(
        self,
        p0: float = KALMAN_INITIAL_P,
        q: float = KALMAN_Q,
        r: float = KALMAN_R,
    ) -> None:
        if not all(math.isfinite(v) for v in (p0, q, r)):
            raise ValueError("Kalman parameters must be finite")
        if p0 <= 0.0 or q < 0.0 or r <= 0.0:
            raise ValueError("Require p0 > 0, q >= 0, and r > 0")

        self.p0 = float(p0)
        self.q = float(q)
        self.r = float(r)

        self.x: Optional[float] = None
        self.p = self.p0
        self.last_time: Optional[float] = None
        self.gain = 0.0

    def update(self, measurement: float, now: float) -> float:
        """Masukkan pengukuran baru dan kembalikan estimasi kecepatan."""

        if not math.isfinite(measurement) or measurement < 0.0:
            raise ValueError("GPS speed must be finite and non-negative")

        if not math.isfinite(now):
            raise ValueError("Timestamp must be finite")

        # Pengukuran valid pertama menjadi inisialisasi state.
        if self.x is None or self.last_time is None:
            self.x = float(measurement)
            self.p = self.p0
            self.last_time = now
            self.gain = 0.0
            return self.x

        dt = max(0.0, now - self.last_time)
        self.last_time = now

        # Prediction: model kecepatan konstan dengan ketidakpastian proses.
        x_pred = self.x
        p_pred = self.p + self.q * dt

        # Correction.
        gain = p_pred / (p_pred + self.r)
        self.x = x_pred + gain * (measurement - x_pred)
        self.p = max(0.0, (1.0 - gain) * p_pred)
        self.gain = gain

        return max(0.0, self.x)

    def reset(self) -> None:
        """Hapus state lama agar sesi fix baru tidak mewarisi estimasi lama."""
        self.x = None
        self.p = self.p0
        self.last_time = None
        self.gain = 0.0


class GPSReader:
    """
    Membaca sentence $GNRMC/$GPRMC pada background thread.

    Status:
        OK     : fix valid dan data lebih baru dari GPS_STALE_TIMEOUT_S
        STALE  : fix terakhir valid, tetapi tidak ada pembaruan tepat waktu
        NOFIX  : sentence RMC menyatakan status V atau belum memperoleh fix
        ERROR  : port serial tidak tersedia / terjadi serial error

    Properti publik aman dibaca dari thread lain.
    """

    def __init__(
        self,
        port: str = GPS_PORT,
        baud: int = GPS_BAUD,
    ) -> None:
        self._port = port
        self._baud = baud

        self._speed_ms: float = 0.0
        self._raw_speed_ms: float = 0.0
        self._last_update: float = 0.0

        self._nmea_fix: str = "V"
        self._serial_ok: bool = False

        self._kalman = KalmanSpeedFilter()

        self._lock = threading.Lock()
        self._stop_ev = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Internal state / validity
    # ------------------------------------------------------------------

    def _is_valid_locked(self) -> bool:
        if not self._serial_ok or self._nmea_fix != "A":
            return False
        if self._last_update <= 0.0:
            return False
        return (time.monotonic() - self._last_update) <= GPS_STALE_TIMEOUT_S

    def _reset_measurement_locked(self, fix: str = "V") -> None:
        self._nmea_fix = fix
        self._last_update = 0.0
        self._speed_ms = 0.0
        self._raw_speed_ms = 0.0
        self._kalman.reset()

    # ------------------------------------------------------------------
    # Public properties — preserve the old pipeline interface
    # ------------------------------------------------------------------

    @property
    def ego_speed_ms(self) -> float:
        """Kecepatan hasil Kalman (m/s), atau 0.0 jika GPS tidak valid."""
        with self._lock:
            return self._speed_ms if self._is_valid_locked() else 0.0

    @property
    def raw_speed_ms(self) -> float:
        """Kecepatan GPS sebelum filtering (m/s), atau 0.0 jika invalid."""
        with self._lock:
            return self._raw_speed_ms if self._is_valid_locked() else 0.0

    @property
    def kalman_gain(self) -> float:
        """Gain terakhir; 0.0 setelah filter diinisialisasi ulang."""
        with self._lock:
            return self._kalman.gain

    @property
    def gps_status(self) -> str:
        with self._lock:
            if not self._serial_ok:
                return "ERROR"
            if self._nmea_fix != "A":
                return "NOFIX"
            if self._last_update <= 0.0:
                return "NOFIX"
            if (time.monotonic() - self._last_update) > GPS_STALE_TIMEOUT_S:
                return "STALE"
            return "OK"

    @property
    def is_valid(self) -> bool:
        with self._lock:
            return self._is_valid_locked()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Mulai thread pembaca; pemanggilan berulang tidak membuat thread ganda."""
        if self._thread is not None and self._thread.is_alive():
            return

        with self._lock:
            self._serial_ok = False
            self._reset_measurement_locked("V")

        self._stop_ev.clear()
        self._thread = threading.Thread(
            target=self._reader_loop,
            name="gps-reader",
            daemon=True,
        )
        self._thread.start()
        log.info("[GPS] Thread started (%s @ %d baud)", self._port, self._baud)

    def stop(self) -> None:
        """Hentikan thread dan tunggu pembacaan serial selesai."""
        self._stop_ev.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            if self._thread.is_alive():
                log.warning("[GPS] Reader thread belum berhenti setelah timeout")
        log.info("[GPS] Thread stopped")

    # ------------------------------------------------------------------
    # NMEA checksum
    # ------------------------------------------------------------------

    @staticmethod
    def _checksum_valid(line: str) -> bool:
        # Beberapa perangkat/stream dapat mengirim sentence tanpa checksum.
        # Jika checksum tersedia, checksum wajib cocok.
        if "*" not in line:
            return True

        try:
            sentence, received = line[1:].rsplit("*", 1)
            calculated = 0
            for char in sentence:
                calculated ^= ord(char)
            return f"{calculated:02X}" == received.strip().upper()
        except (ValueError, IndexError):
            return False

    # ------------------------------------------------------------------
    # Reader thread
    # ------------------------------------------------------------------

    def _reader_loop(self) -> None:
        while not self._stop_ev.is_set():
            ser = None
            try:
                ser = serial.Serial(
                    port=self._port,
                    baudrate=self._baud,
                    timeout=1.0,
                    bytesize=serial.EIGHTBITS,
                    parity=serial.PARITY_NONE,
                    stopbits=serial.STOPBITS_ONE,
                )
                ser.reset_input_buffer()

                with self._lock:
                    self._serial_ok = True
                    self._reset_measurement_locked("V")

                log.info("[GPS] Serial port opened: %s", self._port)

                while not self._stop_ev.is_set():
                    raw = ser.readline()
                    if not raw:
                        # gps_status akan berubah menjadi STALE melalui timeout
                        # tanpa perlu menganggap timeout pembacaan sebagai 0 m/s.
                        continue

                    line = raw.decode("ascii", errors="ignore").strip()
                    if not line.startswith("$"):
                        continue
                    if not self._checksum_valid(line):
                        log.debug("[GPS] NMEA checksum mismatch; sentence skipped")
                        continue

                    if line.startswith(("$GNRMC,", "$GPRMC,")):
                        self._parse_rmc(line)

            except serial.SerialException as exc:
                if not self._stop_ev.is_set():
                    log.warning("[GPS] Serial error: %s", exc)
            except Exception:
                if not self._stop_ev.is_set():
                    log.exception("[GPS] Unexpected reader error")
            finally:
                if ser is not None:
                    try:
                        ser.close()
                    except Exception:
                        pass

                with self._lock:
                    self._serial_ok = False
                    self._reset_measurement_locked("V")

                if not self._stop_ev.is_set():
                    self._stop_ev.wait(5.0)

    # ------------------------------------------------------------------
    # RMC parser + Kalman
    # ------------------------------------------------------------------

    def _parse_rmc(self, sentence: str) -> None:
        parts = sentence.split(",")
        if len(parts) < 8:
            return

        fix_status = parts[2].strip().upper()

        if fix_status != "A":
            with self._lock:
                self._reset_measurement_locked("V")
            return

        # Field 7 adalah speed over ground dalam knots.
        # Field kosong/tidak numerik tidak diperlakukan sebagai 0 m/s,
        # karena itu bukan pengukuran kecepatan yang sah.
        try:
            if not parts[7].strip():
                return
            speed_knots = float(parts[7])
            speed_raw_ms = speed_knots * KNOTS_TO_MS
            if not math.isfinite(speed_raw_ms) or speed_raw_ms < 0.0:
                return
        except (ValueError, TypeError):
            return

        now = time.monotonic()

        with self._lock:
            # Jika data sebelumnya sudah melewati stale timeout, jangan
            # menggabungkan sesi pengukuran baru dengan state filter lama.
            if (
                self._last_update > 0.0
                and (now - self._last_update) > GPS_STALE_TIMEOUT_S
            ):
                self._kalman.reset()

            filtered_ms = self._kalman.update(speed_raw_ms, now)

            self._raw_speed_ms = round(speed_raw_ms, 3)
            self._speed_ms = round(filtered_ms, 3)
            self._last_update = now
            self._nmea_fix = "A"

            log.debug(
                "[GPS] raw=%.3f m/s | filtered=%.3f m/s | gain=%.3f",
                self._raw_speed_ms,
                self._speed_ms,
                self._kalman.gain,
            )

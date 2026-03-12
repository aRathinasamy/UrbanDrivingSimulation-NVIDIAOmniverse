import carb
import omni.usd
import traceback

from omni.kit.scripting import BehaviorScript
from pxr import Sdf


def _get_intersection_registry() -> dict:
    if not hasattr(carb, "_intersection_registry"):
        carb._intersection_registry = {}
    return carb._intersection_registry


class Allstopintersection(BehaviorScript):
    """
    Attach to an intersection root prim.

    Cars approaching call register_arrival() when they reach the stop line,
    poll can_proceed() each frame, then call clear() when they move off.

    USD attributes (auto-created, read on Play):
        stopDuration    Float  2.0  — seconds car must wait at stop line
        clearanceDelay  Float  1.5  — gap between consecutive cars entering
    """

    def on_init(self):
        carb.log_info(f"{type(self).__name__}.on_init()->{self.prim_path}")
        try:
            self._ready = False

            self._stage = omni.usd.get_context().get_stage()
            if self._stage is None:
                carb.log_error("[INT] Stage is None")
                return

            root_prim = self._stage.GetPrimAtPath(self.prim_path)
            if not root_prim or not root_prim.IsValid():
                carb.log_error(f"[INT] Prim not found: {self.prim_path}")
                return

            self._root_prim = root_prim

            # Create attrs so they appear in Properties panel — read in on_play
            self._ensure_float("stopDuration",   2.0)
            self._ensure_float("clearanceDelay", 1.5)

            self._stop_duration     = 2.0
            self._clearance_delay   = 1.5
            self._queue             = []
            self._last_cleared_time = -999.0

            _get_intersection_registry()[str(self.prim_path)] = self
            carb.log_warn(f"[INT] Registered: {self.prim_path}")

            self._ready = True

        except Exception:
            carb.log_error("[INT] EXCEPTION in on_init")
            carb.log_error(traceback.format_exc())

    def on_play(self):
        self._stop_duration     = self._read_float("stopDuration",   2.0)
        self._clearance_delay   = self._read_float("clearanceDelay", 1.5)
        self._queue             = []
        self._last_cleared_time = -999.0
        carb.log_warn(
            f"[INT] on_play {self.prim_path} — "
            f"stopDuration={self._stop_duration}s  "
            f"clearanceDelay={self._clearance_delay}s"
        )

    def on_stop(self):
        self._queue             = []
        self._last_cleared_time = -999.0

    def on_pause(self): pass
    def on_update(self, current_time, dt): pass

    def on_destroy(self):
        _get_intersection_registry().pop(str(self.prim_path), None)

    # ------------------------------------------------------------------
    # Public API — called by VehicleController
    # ------------------------------------------------------------------

    def register_arrival(self, prim_path: str, current_time: float):
        """Add car to queue on first arrival at stop line."""
        for entry in self._queue:
            if entry[1] == prim_path:
                return  # already registered

        self._queue.append((current_time, prim_path))
        # Sort by arrival time; ties broken by prim_path (right-of-way)
        self._queue.sort(key=lambda e: (e[0], e[1]))

        carb.log_warn(
            f"[INT] {prim_path.split('/')[-1]} arrived  "
            f"queue={[e[1].split('/')[-1] for e in self._queue]}"
        )

    def can_proceed(self, prim_path: str, current_time: float) -> bool:
        """
        True when:
          1. This car is first in queue
          2. Has waited >= stopDuration
          3. >= clearanceDelay since last car proceeded
        """
        if not self._queue:
            return False

        first_time, first_car = self._queue[0]

        if first_car != prim_path:
            return False

        waited    = current_time - first_time
        gap_clear = current_time - self._last_cleared_time

        return waited >= self._stop_duration and gap_clear >= self._clearance_delay

    def clear(self, prim_path: str, current_time: float):
        """Remove car from queue when it starts crossing."""
        self._queue = [e for e in self._queue if e[1] != prim_path]
        self._last_cleared_time = current_time
        carb.log_warn(
            f"[INT] {prim_path.split('/')[-1]} cleared  "
            f"queue={[e[1].split('/')[-1] for e in self._queue]}"
        )

    # ------------------------------------------------------------------
    def _read_float(self, name: str, default: float) -> float:
        try:
            attr = self._root_prim.GetAttribute(name)
            if attr and attr.IsValid():
                val = attr.Get()
                if val is not None:
                    return float(val)
        except Exception:
            pass
        return default

    def _ensure_float(self, name: str, default: float):
        attr = self._root_prim.GetAttribute(name)
        if not attr or not attr.IsValid():
            attr = self._root_prim.CreateAttribute(name, Sdf.ValueTypeNames.Float, False)
            attr.Set(float(default))
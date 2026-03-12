import carb
import omni.usd
import traceback

from omni.kit.scripting import BehaviorScript
from pxr import Gf, Sdf, UsdShade


class Trafficlight(BehaviorScript):
    """
    Attach to any traffic light root prim.

    Expected hierarchy (same child names for ALL lights):
        <TL_root>
            Green   (Sphere, has material)
            Yellow  (Sphere, has material)
            Red     (Sphere, has material)

    USD attributes on the root prim (auto-created with defaults):
        greenDuration  (Float) — seconds for green phase  default 6.0
        yellowDuration (Float) — seconds for yellow phase default 2.0
        redDuration    (Float) — seconds for red phase    default 6.0
        phaseOffset    (Float) — pre-advance timer in seconds,
                                 use to offset lights at same intersection
                                 default 0.0
        startState     (Token) — initial state to start from on Play:
                                 GREEN / YELLOW / RED (default GREEN)
        currentState   (Token) — runtime published state for vehicles to read
    """

    # Common child names — same for every traffic light in the scene
    _BULB_GREEN = "Green"
    _BULB_YELLOW = "Yellow"
    _BULB_RED = "Red"

    def on_init(self):
        carb.log_info(f"{type(self).__name__}.on_init()->{self.prim_path}")
        try:
            # Colors defined in on_init — avoids BehaviorScript import crash
            self._ON_GREEN = Gf.Vec3f(0.0, 1.0, 0.0)
            self._ON_YELLOW = Gf.Vec3f(1.0, 0.8, 0.0)
            self._ON_RED = Gf.Vec3f(1.0, 0.0, 0.0)
            self._OFF_GREEN = Gf.Vec3f(0.0, 0.05, 0.0)
            self._OFF_YELLOW = Gf.Vec3f(0.05, 0.04, 0.0)
            self._OFF_RED = Gf.Vec3f(0.05, 0.0, 0.0)

            self._ready = False

            self._stage = omni.usd.get_context().get_stage()
            if self._stage is None:
                carb.log_error("[TL] Stage is None")
                return

            root = Sdf.Path(self.prim_path)
            root_prim = self._stage.GetPrimAtPath(root)

            if not root_prim or not root_prim.IsValid():
                carb.log_error(f"[TL] Root prim not found: {root}")
                return

            # ---- Resolve bulb prims by common child names ----
            self._green = self._stage.GetPrimAtPath(root.AppendChild(self._BULB_GREEN))
            self._yellow = self._stage.GetPrimAtPath(root.AppendChild(self._BULB_YELLOW))
            self._red = self._stage.GetPrimAtPath(root.AppendChild(self._BULB_RED))

            missing = [
                name
                for name, p in [
                    (self._BULB_GREEN, self._green),
                    (self._BULB_YELLOW, self._yellow),
                    (self._BULB_RED, self._red),
                ]
                if not (p and p.IsValid())
            ]
            if missing:
                carb.log_error(
                    f"[TL] Missing bulb prims {missing} under {root}. "
                    f"Rename your bulb children to: "
                    f"'{self._BULB_GREEN}', '{self._BULB_YELLOW}', '{self._BULB_RED}'"
                )
                return

            # Keep reference for on_play reads
            self._root_prim = root_prim

            # Ensure attributes exist so they appear in Properties panel.
            # Actual values are read in on_play.
            self._read_float(root_prim, "greenDuration", 6.0)
            self._read_float(root_prim, "yellowDuration", 2.0)
            self._read_float(root_prim, "redDuration", 6.0)
            self._read_float(root_prim, "phaseOffset", 0.0)

            # NEW: UI-configurable start state
            self._read_token(root_prim, "startState", "GREEN")

            # Defaults — overwritten in on_play
            self._dur_green = 6.0
            self._dur_yellow = 2.0
            self._dur_red = 6.0

            # Initialize state from UI so the viewport reflects it even before Play
            start_state = self._read_token(root_prim, "startState", "GREEN").upper()
            if start_state not in ("GREEN", "YELLOW", "RED"):
                start_state = "GREEN"

            self._state = start_state
            self._elapsed = 0.0

            # Publish state into USD so vehicles can read it directly
            self._state_attr = root_prim.GetAttribute("currentState")
            if not self._state_attr or not self._state_attr.IsValid():
                self._state_attr = root_prim.CreateAttribute("currentState", Sdf.ValueTypeNames.Token, False)
            self._state_attr.Set(self._state)

            self._apply(self._state)

            self._ready = True
            carb.log_warn(f"[TL] Ready — {self._state} (durations/startState/offset will be read on Play)")

        except Exception:
            carb.log_error("[TL] EXCEPTION in on_init")
            carb.log_error(traceback.format_exc())

    def on_destroy(self):
        carb.log_info(f"{type(self).__name__}.on_destroy()->{self.prim_path}")

    def on_play(self):
        try:
            self._dur_green = self._read_float(self._root_prim, "greenDuration", 6.0)
            self._dur_yellow = self._read_float(self._root_prim, "yellowDuration", 2.0)
            self._dur_red = self._read_float(self._root_prim, "redDuration", 6.0)
            offset = self._read_float(self._root_prim, "phaseOffset", 0.0)

            # Read start state from UI
            start_state = self._read_token(self._root_prim, "startState", "GREEN").upper()
            if start_state not in ("GREEN", "YELLOW", "RED"):
                carb.log_warn(f"[TL] Invalid startState='{start_state}', defaulting to GREEN")
                start_state = "GREEN"

            self._state = start_state
            self._elapsed = 0.0

            # Apply chosen start state
            self._apply(self._state)
            if self._state_attr and self._state_attr.IsValid():
                self._state_attr.Set(self._state)

            # If phaseOffset is used, pre-advance the cycle robustly across states
            if offset and offset > 0.0:
                self._advance_by_offset(offset)

            carb.log_warn(
                f"[TL] on_play {self.prim_path} — "
                f"START={start_state}  "
                f"G={self._dur_green}s  Y={self._dur_yellow}s  R={self._dur_red}s  "
                f"offset={offset}s"
            )
        except Exception:
            carb.log_error("[TL] EXCEPTION in on_play")
            carb.log_error(traceback.format_exc())

    def on_pause(self):
        carb.log_info(f"{type(self).__name__}.on_pause()->{self.prim_path}")

    def on_stop(self):
        if hasattr(self, "_elapsed"):
            self._elapsed = 0.0
        carb.log_info(f"{type(self).__name__}.on_stop()->{self.prim_path}")

    def on_update(self, current_time: float, dt: float):
        try:
            if not getattr(self, "_ready", False):
                return
            if not dt or dt <= 0.0:
                return

            self._elapsed += dt

            if self._state == "GREEN" and self._elapsed >= self._dur_green:
                self._transition("YELLOW")
            elif self._state == "YELLOW" and self._elapsed >= self._dur_yellow:
                self._transition("RED")
            elif self._state == "RED" and self._elapsed >= self._dur_red:
                self._transition("GREEN")

        except Exception:
            carb.log_error("[TL] EXCEPTION in on_update")
            carb.log_error(traceback.format_exc())

    # ------------------------------------------------------------------
    # Public API — polled by VehicleController
    # ------------------------------------------------------------------
    def get_phase(self) -> str:
        """Returns 'GREEN', 'YELLOW', or 'RED'."""
        return getattr(self, "_state", "RED")

    # ------------------------------------------------------------------
    def _transition(self, new_state: str):
        self._state = new_state
        if hasattr(self, "_state_attr") and self._state_attr and self._state_attr.IsValid():
            self._state_attr.Set(self._state)
        self._elapsed = 0.0
        self._apply(new_state)
        carb.log_warn(f"[TL] {self.prim_path} -> {new_state}")

    def _advance_by_offset(self, seconds: float):
        """Consume offset across states so phaseOffset behaves correctly."""
        remaining = float(seconds)
        while remaining > 0.0:
            dur = self._dur_green if self._state == "GREEN" else (self._dur_yellow if self._state == "YELLOW" else self._dur_red)
            to_go = max(dur - self._elapsed, 0.0)

            if remaining < to_go:
                self._elapsed += remaining
                remaining = 0.0
            else:
                remaining -= to_go
                # Move to next state
                next_state = "YELLOW" if self._state == "GREEN" else ("RED" if self._state == "YELLOW" else "GREEN")
                self._state = next_state
                self._elapsed = 0.0
                self._apply(self._state)
                if self._state_attr and self._state_attr.IsValid():
                    self._state_attr.Set(self._state)

    def _apply(self, state: str):
        if state == "GREEN":
            self._set_albedo(self._green, self._ON_GREEN)
            self._set_albedo(self._yellow, self._OFF_YELLOW)
            self._set_albedo(self._red, self._OFF_RED)
        elif state == "YELLOW":
            self._set_albedo(self._green, self._OFF_GREEN)
            self._set_albedo(self._yellow, self._ON_YELLOW)
            self._set_albedo(self._red, self._OFF_RED)
        elif state == "RED":
            self._set_albedo(self._green, self._OFF_GREEN)
            self._set_albedo(self._yellow, self._OFF_YELLOW)
            self._set_albedo(self._red, self._ON_RED)

    def _set_albedo(self, prim, color: Gf.Vec3f):
        if not prim or not prim.IsValid():
            return
        shader = self._find_shader(prim)
        if shader is None:
            return
        inp = shader.GetInput("diffuse_color_constant")
        if not inp:
            inp = shader.CreateInput("diffuse_color_constant", Sdf.ValueTypeNames.Color3f)
        inp.Set(Gf.Vec3f(color))

    def _find_shader(self, prim):
        for candidate in [prim] + list(prim.GetChildren()):
            try:
                mat_path = (
                    UsdShade.MaterialBindingAPI(candidate)
                    .GetDirectBinding()
                    .GetMaterialPath()
                )
                if not mat_path or mat_path.isEmpty:
                    continue
                mat_prim = self._stage.GetPrimAtPath(mat_path)
                if not mat_prim or not mat_prim.IsValid():
                    continue
                material = UsdShade.Material(mat_prim)
                for out_name in ("mdl:surface", "surface"):
                    output = material.GetOutput(out_name)
                    if not output:
                        continue
                    for item in (output.GetConnectedSources() or []):
                        for src in (item if isinstance(item, (list, tuple)) else [item]):
                            sp = src.source.GetPrim() if hasattr(src, "source") else None
                            if sp and sp.IsValid():
                                return UsdShade.Shader(sp)
                for child in mat_prim.GetChildren():
                    if child.GetTypeName() == "Shader":
                        return UsdShade.Shader(child)
            except Exception:
                continue
        return None

    def _read_float(self, prim, name: str, default: float) -> float:
        attr = prim.GetAttribute(name)
        if not attr or not attr.IsValid():
            attr = prim.CreateAttribute(name, Sdf.ValueTypeNames.Float, False)
            attr.Set(float(default))
        val = attr.Get()
        return float(val) if val is not None else default

    def _read_token(self, prim, name: str, default: str) -> str:
        attr = prim.GetAttribute(name)
        if not attr or not attr.IsValid():
            attr = prim.CreateAttribute(name, Sdf.ValueTypeNames.Token, False)
            attr.Set(str(default))
        val = attr.Get()
        return str(val) if val is not None else str(default)
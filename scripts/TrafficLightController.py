import carb
from omni.kit.scripting import BehaviorScript
from pxr import UsdGeom, UsdShade, Sdf, Gf
import omni.usd
import traceback

carb.log_warn("[TrafficLightController] MODULE LOADED v4")


class TrafficLightController(BehaviorScript):
    """
    Drives traffic light bulb materials via OmniPBR emissive inputs.
    No class-level Gf constants — all defined inside on_init to avoid
    BehaviorScript import-time crash with Gf not yet initialized.
    """

    def on_init(self):
        try:
            self._ready = False

            # ---- Define colors here (NOT at class level) ----
            self._COLOR_OFF       = Gf.Vec3f(0.0, 0.0, 0.0)
            self._COLOR_GREEN_ON  = Gf.Vec3f(0.0, 1.0, 0.0)
            self._COLOR_YELLOW_ON = Gf.Vec3f(1.0, 0.8, 0.0)
            self._COLOR_RED_ON    = Gf.Vec3f(1.0, 0.0, 0.0)

            carb.log_info(f"[TLC] on_init -> {self.prim_path}")

            self._stage = omni.usd.get_context().get_stage()
            if self._stage is None:
                carb.log_error("[TLC] Stage is None")
                return

            self._prim = self._stage.GetPrimAtPath(self.prim_path)
            if not self._prim or not self._prim.IsValid():
                carb.log_error(f"[TLC] Invalid prim: {self.prim_path}")
                return

            # ---- Attributes ----
            self._green_path_attr  = self._get_or_create_string_attr("greenBulbPath",  "")
            self._yellow_path_attr = self._get_or_create_string_attr("yellowBulbPath", "")
            self._red_path_attr    = self._get_or_create_string_attr("redBulbPath",    "")
            self._green_time_attr  = self._get_or_create_float_attr("greenDuration",   6.0)
            self._yellow_time_attr = self._get_or_create_float_attr("yellowDuration",  2.0)
            self._red_time_attr    = self._get_or_create_float_attr("redDuration",     6.0)
            self._start_state_attr = self._get_or_create_string_attr("startState",     "GREEN")

            # ---- Resolve bulb prims ----
            self._green_bulb  = self._resolve_prim(self._green_path_attr.Get()  or "")
            self._yellow_bulb = self._resolve_prim(self._yellow_path_attr.Get() or "")
            self._red_bulb    = self._resolve_prim(self._red_path_attr.Get()    or "")

            if not (self._green_bulb and self._yellow_bulb and self._red_bulb):
                carb.log_error(f"[TLC] One or more bulb prims not found on {self.prim_path}")
                return

            # ---- State machine ----
            raw = self._start_state_attr.Get() or "GREEN"
            self._state = str(raw).upper().strip()
            if self._state not in ("GREEN", "YELLOW", "RED"):
                self._state = "GREEN"

            self._t = 0.0
            self._apply_state(self._state)
            self._ready = True

            carb.log_info(f"[TLC] Ready  state={self._state}")

        except Exception:
            carb.log_error("[TLC] EXCEPTION in on_init")
            carb.log_error(traceback.format_exc())

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def on_destroy(self):
        carb.log_info(f"[TLC] on_destroy {self.prim_path}")

    def on_play(self):
        carb.log_info(f"[TLC] on_play {self.prim_path}")

    def on_pause(self):
        carb.log_info(f"[TLC] on_pause {self.prim_path}")

    def on_stop(self):
        carb.log_info(f"[TLC] on_stop {self.prim_path}")
        if hasattr(self, "_t"):
            self._t = 0.0

    # ------------------------------------------------------------------ #
    # Tick
    # ------------------------------------------------------------------ #
    def on_update(self, dt: float):
        try:
            if not getattr(self, "_ready", False):
                return
            if not dt or dt <= 0.0:
                return
            if not self._prim.IsValid():
                return
            if not (self._green_bulb.IsValid()
                    and self._yellow_bulb.IsValid()
                    and self._red_bulb.IsValid()):
                return

            g = max(float(self._green_time_attr.Get()  or 6.0), 0.1)
            y = max(float(self._yellow_time_attr.Get() or 2.0), 0.1)
            r = max(float(self._red_time_attr.Get()    or 6.0), 0.1)

            self._t += float(dt)

            if   self._state == "GREEN"  and self._t >= g: self._set_state("YELLOW")
            elif self._state == "YELLOW" and self._t >= y: self._set_state("RED")
            elif self._state == "RED"    and self._t >= r: self._set_state("GREEN")

        except Exception:
            carb.log_error("[TLC] EXCEPTION in on_update")
            carb.log_error(traceback.format_exc())

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def get_phase(self) -> str:
        return getattr(self, "_state", "RED")

    # ------------------------------------------------------------------ #
    # State
    # ------------------------------------------------------------------ #
    def _set_state(self, s: str):
        self._state = s
        self._t = 0.0
        self._apply_state(s)
        carb.log_info(f"[TLC] {self.prim_path} -> {s}")

    def _apply_state(self, state: str):
        off = self._COLOR_OFF
        if state == "GREEN":
            self._set_bulb(self._green_bulb,  self._COLOR_GREEN_ON,  True)
            self._set_bulb(self._yellow_bulb, off,                   False)
            self._set_bulb(self._red_bulb,    off,                   False)
        elif state == "YELLOW":
            self._set_bulb(self._green_bulb,  off,                   False)
            self._set_bulb(self._yellow_bulb, self._COLOR_YELLOW_ON, True)
            self._set_bulb(self._red_bulb,    off,                   False)
        elif state == "RED":
            self._set_bulb(self._green_bulb,  off,                   False)
            self._set_bulb(self._yellow_bulb, off,                   False)
            self._set_bulb(self._red_bulb,    self._COLOR_RED_ON,    True)

    # ------------------------------------------------------------------ #
    # Bulb material control
    # ------------------------------------------------------------------ #
    def _set_bulb(self, bulb_prim, color: Gf.Vec3f, is_on: bool):
        if not bulb_prim or not bulb_prim.IsValid():
            return

        material_set = False

        try:
            for prim in [bulb_prim] + list(bulb_prim.GetChildren()):
                shader = self._find_shader(prim)
                if not shader:
                    continue

                emit_color = shader.GetInput("emissive_color")
                if not emit_color:
                    emit_color = shader.CreateInput("emissive_color", Sdf.ValueTypeNames.Color3f)
                emit_color.Set(Gf.Vec3f(color if is_on else self._COLOR_OFF))

                emit_intensity = shader.GetInput("emissive_intensity")
                if not emit_intensity:
                    emit_intensity = shader.CreateInput("emissive_intensity", Sdf.ValueTypeNames.Float)
                emit_intensity.Set(1.0 if is_on else 0.0)

                enable = shader.GetInput("enable_emission")
                if enable:
                    enable.Set(is_on)

                material_set = True
                break

        except Exception:
            carb.log_warn(f"[TLC] Material set failed on {bulb_prim.GetPath()}")
            carb.log_warn(traceback.format_exc())

        # Fallback to displayColor
        if not material_set:
            try:
                gprim = UsdGeom.Gprim(bulb_prim)
                if gprim:
                    attr = gprim.GetDisplayColorAttr()
                    if attr and attr.IsValid():
                        c = color if is_on else Gf.Vec3f(0.05, 0.05, 0.05)
                        attr.Set([c])
            except Exception:
                pass

    def _find_shader(self, prim):

        try:
            binding = UsdShade.MaterialBindingAPI(prim)
            if not binding:
                return None
            bound    = binding.GetDirectBinding()
            mat_path = bound.GetMaterialPath() if bound else None
            if not mat_path or mat_path.isEmpty:
                return None
            material = UsdShade.Material(self._stage.GetPrimAtPath(mat_path))
            if not material:
                return None

            # Try surface outputs
            for out_name in ("surface", "mdl:surface"):
                output = material.GetOutput(out_name)
                if not output:
                    continue
                connected = output.GetConnectedSources()
                if not connected:
                    continue
                for item in connected:
                    srcs = item if isinstance(item, (list, tuple)) else [item]
                    for src in srcs:
                        p = src.source.GetPrim() if hasattr(src, "source") else None
                        if p and p.IsValid():
                            s = UsdShade.Shader(p)
                            if s:
                                return s

            # Fallback: any Shader child
            for child in self._stage.GetPrimAtPath(mat_path).GetChildren():
                if child.GetTypeName() == "Shader":
                    return UsdShade.Shader(child)

        except Exception:
            pass
        return None

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _get_or_create_string_attr(self, name, default):
        attr = self._prim.GetAttribute(name)
        if not attr or not attr.IsValid():
            attr = self._prim.CreateAttribute(name, Sdf.ValueTypeNames.String, False)
            attr.Set(default)
        return attr

    def _get_or_create_float_attr(self, name, default):
        attr = self._prim.GetAttribute(name)
        if not attr or not attr.IsValid():
            attr = self._prim.CreateAttribute(name, Sdf.ValueTypeNames.Float, False)
            attr.Set(float(default))
        return attr

    def _resolve_prim(self, path_str):
        if not path_str or not path_str.strip():
            return None
        try:
            p = self._stage.GetPrimAtPath(Sdf.Path(path_str.strip()))
            return p if (p and p.IsValid()) else None
        except Exception:
            carb.log_error(f"[TLC] Bad path: '{path_str}'")
            return None
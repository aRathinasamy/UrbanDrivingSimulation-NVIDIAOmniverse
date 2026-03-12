"""
FreeCamera.py — Attach to a Camera prim in Omniverse.

Uses omni.kit.app input events (subscription-based) — works across all Kit versions.

Controls:
    W / S   — move forward / backward
    A / D   — strafe left / right
    Q / E   — move down / up
    Shift   — hold for 3x speed
    (Use Omniverse built-in right mouse button to look around)

USD attributes (auto-created, read on Play):
    moveSpeed        Float  5.0   — units/second base speed
    smoothing        Float  8.0   — movement smoothing (higher = snappier)
"""

import carb
import carb.input
import math
import omni.appwindow
import omni.usd
import traceback

from omni.kit.scripting import BehaviorScript
from pxr import Gf, Sdf, UsdGeom


class Freecamera(BehaviorScript):

    def on_init(self):
        carb.log_info(f"{type(self).__name__}.on_init() -> {self.prim_path}")
        try:
            self._ready = False

            self._stage = omni.usd.get_context().get_stage()
            if self._stage is None:
                carb.log_error("[FC] Stage is None")
                return

            root_prim = self._stage.GetPrimAtPath(self.prim_path)
            if not root_prim or not root_prim.IsValid():
                carb.log_error(f"[FC] Prim not found: {self.prim_path}")
                return

            self._root_prim = root_prim

            # USD attributes
            self._ensure_attr("moveSpeed",        Sdf.ValueTypeNames.Float, 5.0)
            self._ensure_attr("smoothing",         Sdf.ValueTypeNames.Float, 8.0)

            # Runtime state
            self._move_speed  = 5.0
            self._smoothing   = 8.0
            self._vel         = Gf.Vec3d(0, 0, 0)

            # Key states — tracked via subscription
            self._keys_held   = set()   # set of KeyboardInput values


            # Subscriptions (registered in on_play, removed in on_stop)
            self._keyboard_sub = None

            # Transform ops
            xformable = UsdGeom.Xformable(root_prim)
            self._translate_op = None
            for op in xformable.GetOrderedXformOps():
                if op.GetOpType() == UsdGeom.XformOp.TypeTranslate:
                    self._translate_op = op

            if self._translate_op is None:
                self._translate_op = xformable.AddTranslateOp(
                    precision=UsdGeom.XformOp.PrecisionDouble)

            self._initial_translate = self._translate_op.Get() or Gf.Vec3d(0, 0, 0)

            self._ready = True
            carb.log_warn("[FC] Initialized")

        except Exception:
            carb.log_error("[FC] EXCEPTION in on_init")
            carb.log_error(traceback.format_exc())

    # ------------------------------------------------------------------ #
    def on_play(self):
        try:
            self._move_speed = self._read_float("moveSpeed",        5.0)
            self._smoothing  = self._read_float("smoothing",         8.0)

            self._vel       = Gf.Vec3d(0, 0, 0)
            self._keys_held = set()

            # Subscribe to input events
            app_window = omni.appwindow.get_default_app_window()
            iinput     = carb.input.acquire_input_interface()

            self._keyboard_sub = iinput.subscribe_to_keyboard_events(
                app_window.get_keyboard(),
                self._on_keyboard_event
            )

            carb.log_warn(
                f"[FC] on_play — speed={self._move_speed}  "
                f"smoothing={self._smoothing}"
            )
        except Exception:
            carb.log_error("[FC] EXCEPTION in on_play")
            carb.log_error(traceback.format_exc())

    def on_stop(self):
        self._unsubscribe()
        self._vel       = Gf.Vec3d(0, 0, 0)
        self._keys_held = set()
        if self._translate_op and self._initial_translate is not None:
            self._translate_op.Set(self._initial_translate)

    def on_destroy(self):
        self._unsubscribe()

    def on_pause(self): pass

    def _unsubscribe(self):
        try:
            iinput = carb.input.acquire_input_interface()
            if self._keyboard_sub is not None:
                iinput.unsubscribe_to_keyboard_events(
                    omni.appwindow.get_default_app_window().get_keyboard(),
                    self._keyboard_sub
                )
                self._keyboard_sub = None
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # Input event callbacks
    # ------------------------------------------------------------------ #
    def _on_keyboard_event(self, event, *args):
        try:
            key = event.input
            if event.type == carb.input.KeyboardEventType.KEY_PRESS:
                self._keys_held.add(key)
            elif event.type == carb.input.KeyboardEventType.KEY_RELEASE:
                self._keys_held.discard(key)
        except Exception:
            pass
        return True   # don't consume — let other handlers also receive it

    # ------------------------------------------------------------------ #
    def on_update(self, current_time: float, dt: float):
        try:
            if not getattr(self, "_ready", False) or dt <= 0.0:
                return

            K = carb.input.KeyboardInput

            # ── Speed modifier ────────────────────────────────────────
            shift = (K.LEFT_SHIFT  in self._keys_held or
                     K.RIGHT_SHIFT in self._keys_held)
            speed = self._move_speed * (3.0 if shift else 1.0)

            # ── Camera axes ───────────────────────────────────────────
            fwd, right, _ = self._camera_axes()

            # ── Desired velocity from keys ────────────────────────────
            move = Gf.Vec3d(0, 0, 0)
            if K.W in self._keys_held: move -= fwd   * speed
            if K.S in self._keys_held: move += fwd   * speed
            if K.D in self._keys_held: move -= right * speed
            if K.A in self._keys_held: move += right * speed
            if K.E in self._keys_held: move += Gf.Vec3d(0, speed, 0)
            if K.Q in self._keys_held: move -= Gf.Vec3d(0, speed, 0)

            # ── Smooth velocity ───────────────────────────────────────
            t = min(self._smoothing * dt, 1.0)
            self._vel = self._vel * (1.0 - t) + move * t
            # Snap to zero when very slow to prevent infinite drift
            if self._vel.GetLength() < 0.001:
                self._vel = Gf.Vec3d(0, 0, 0)

            # ── Apply position ────────────────────────────────────────
            pos = Gf.Vec3d(self._translate_op.Get() or Gf.Vec3d(0, 0, 0))
            pos += self._vel * dt
            self._translate_op.Set(pos)

        except Exception:
            carb.log_error("[FC] EXCEPTION in on_update")
            carb.log_error(traceback.format_exc())

    # ------------------------------------------------------------------ #
    # Camera maths
    # ------------------------------------------------------------------ #
    def _camera_axes(self):
        """
        Read the camera's current world rotation from USD and return
        forward/right/up axes. This lets Omniverse built-in mouse look
        control orientation freely — we only ever touch position.
        """
        xform = UsdGeom.Xformable(self._root_prim)
        world_matrix = xform.ComputeLocalToWorldTransform(0)
        # Extract rotation columns from world matrix
        # Column 0 = right, Column 1 = up, Column 2 = back (-forward)
        m = world_matrix
        right   = Gf.Vec3d(m[0][0], m[1][0], m[2][0]).GetNormalized()
        up      = Gf.Vec3d(m[0][1], m[1][1], m[2][1]).GetNormalized()
        forward = Gf.Vec3d(-m[0][2], -m[1][2], -m[2][2]).GetNormalized()
        return forward, right, up

    def _read_float(self, name: str, default: float) -> float:
        try:
            attr = self._root_prim.GetAttribute(name)
            if attr and attr.IsValid():
                v = attr.Get()
                if v is not None:
                    return float(v)
        except Exception:
            pass
        return default

    def _ensure_attr(self, name: str, type_name, default):
        attr = self._root_prim.GetAttribute(name)
        if not attr or not attr.IsValid():
            attr = self._root_prim.CreateAttribute(name, type_name, False)
            attr.Set(default)
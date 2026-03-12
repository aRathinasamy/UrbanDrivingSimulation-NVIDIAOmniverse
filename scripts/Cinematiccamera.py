import carb
import math
import omni.usd
import traceback

from omni.kit.scripting import BehaviorScript
from pxr import Gf, Sdf, UsdGeom


class Cinematiccamera(BehaviorScript):
    """
    Attach to a Camera prim.

    The camera travels along a BasisCurves path and smoothly looks toward
    a sequence of target points defined as Xform prims.

    USD attributes (auto-created with defaults, read on Play):

        curvePath         String   ""       — path to BasisCurves prim (camera rail)
        speed             Float    5.0      — travel speed in scene units/s
        loopCamera        Bool     False    — loop back to start when curve ends
        lookSmoothing     Float    5.0      — rotation lerp speed (higher = snappier)
        targetPaths       StringArray []    — ordered list of Xform prim paths to look at
        targetBlendDist   Float    10.0     — distance before reaching target where
                                             camera starts blending toward next target
        rollAngle         Float    0.0      — camera roll in degrees (cinematic tilt)

    How targets work:
        The camera looks at target[0] from the start.
        When it comes within targetBlendDist of the point on the curve
        closest to target[N], it smoothly blends to look at target[N+1].
        After the last target, the camera keeps looking at the last one.

    Setup:
        1. Add a Camera prim to the stage
        2. Draw a BasisCurves prim as the camera rail (e.g. using the Curve tool)
        3. Add Xform prims at each look-at point (position them in scene)
        4. Attach this script to the Camera prim
        5. Set curvePath, targetPaths, speed in Properties panel
        6. Hit Play
    """

    def on_init(self):
        carb.log_info(f"{type(self).__name__}.on_init()->{self.prim_path}")
        try:
            self._ready = False

            self._stage = omni.usd.get_context().get_stage()
            if self._stage is None:
                carb.log_error("[CAM] Stage is None")
                return

            root_prim = self._stage.GetPrimAtPath(self.prim_path)
            if not root_prim or not root_prim.IsValid():
                carb.log_error(f"[CAM] Camera prim not found: {self.prim_path}")
                return

            self._root_prim = root_prim

            # Ensure all attributes exist with defaults
            self._ensure_attr("curvePath",       Sdf.ValueTypeNames.String,      "")
            self._ensure_attr("speed",            Sdf.ValueTypeNames.Float,       5.0)
            self._ensure_attr("loopCamera",       Sdf.ValueTypeNames.Bool,        False)
            self._ensure_attr("lookSmoothing",    Sdf.ValueTypeNames.Float,       5.0)
            self._ensure_attr("targetPaths",      Sdf.ValueTypeNames.StringArray, [])
            self._ensure_attr("targetBlendDist",  Sdf.ValueTypeNames.Float,       10.0)
            self._ensure_attr("rollAngle",        Sdf.ValueTypeNames.Float,       0.0)

            # Runtime state — overwritten in on_play
            self._curve_points   = []
            self._seg_lengths    = []
            self._total_length   = 0.0
            self._distance       = 0.0
            self._speed          = 5.0
            self._loop           = False
            self._smoothing      = 5.0
            self._blend_dist     = 10.0
            self._roll_angle     = 0.0
            self._target_positions = []
            self._current_target_idx = 0
            self._current_look   = None   # Gf.Vec3d — current look direction
            self._active         = False

            # Get transform ops
            xformable = UsdGeom.Xformable(root_prim)
            self._translate_op = None
            self._rotate_op    = None

            for op in xformable.GetOrderedXformOps():
                if op.GetOpType() == UsdGeom.XformOp.TypeTranslate:
                    self._translate_op = op
                elif op.GetOpType() == UsdGeom.XformOp.TypeOrient:
                    self._rotate_op = op
                elif op.GetOpType() == UsdGeom.XformOp.TypeRotateXYZ:
                    self._rotate_op = op

            if self._translate_op is None:
                self._translate_op = xformable.AddTranslateOp(
                    precision=UsdGeom.XformOp.PrecisionDouble
                )
            if self._rotate_op is None:
                self._rotate_op = xformable.AddOrientOp(
                    precision=UsdGeom.XformOp.PrecisionDouble
                )

            # Store initial transform to restore on stop
            self._initial_translate = self._translate_op.Get()
            self._initial_rotate    = self._rotate_op.Get()

            self._ready = True
            carb.log_warn("[CAM] Initialized — values will be read on Play")

        except Exception:
            carb.log_error("[CAM] EXCEPTION in on_init")
            carb.log_error(traceback.format_exc())

    # ------------------------------------------------------------------
    def on_play(self):
        try:
            if not getattr(self, "_ready", False):
                return

            # Read all attributes from Properties panel
            self._speed       = self._read_float("speed",           5.0)
            self._loop        = self._read_bool ("loopCamera",      False)
            self._smoothing   = self._read_float("lookSmoothing",   5.0)
            self._blend_dist  = self._read_float("targetBlendDist", 10.0)
            self._roll_angle  = self._read_float("rollAngle",       0.0)
            curve_path        = self._read_string("curvePath",      "")
            target_paths      = self._read_string_array("targetPaths", [])

            # Load camera rail curve
            self._curve_points = []
            self._seg_lengths  = []
            self._total_length = 0.0

            if curve_path:
                prim = self._stage.GetPrimAtPath(curve_path)
                if prim and prim.IsValid() and prim.IsA(UsdGeom.BasisCurves):
                    curve   = UsdGeom.BasisCurves(prim)
                    points  = curve.GetPointsAttr().Get()
                    if points and len(points) >= 2:
                        world_matrix = UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(0)
                        self._curve_points = [
                            world_matrix.Transform(Gf.Vec3d(p)) for p in points
                        ]
                        for i in range(len(self._curve_points) - 1):
                            seg = (self._curve_points[i+1] - self._curve_points[i]).GetLength()
                            self._seg_lengths.append(max(seg, 1e-6))
                            self._total_length += self._seg_lengths[-1]
                        carb.log_warn(f"[CAM] Rail loaded: {len(self._curve_points)} pts, length={self._total_length:.1f}")
                    else:
                        carb.log_error(f"[CAM] Curve has < 2 points: {curve_path}")
                else:
                    carb.log_error(f"[CAM] curvePath not a valid BasisCurves: {curve_path}")
            else:
                carb.log_error("[CAM] curvePath is empty — camera will not move")

            # Load target world positions
            self._target_positions = []
            for tp in target_paths:
                tp = tp.strip()
                if not tp:
                    continue
                p = self._stage.GetPrimAtPath(tp)
                if p and p.IsValid():
                    xf = UsdGeom.Xformable(p).ComputeLocalToWorldTransform(0)
                    pos = xf.ExtractTranslation()
                    self._target_positions.append(Gf.Vec3d(pos))
                    carb.log_info(f"[CAM] Target: {tp}  pos={pos}")
                else:
                    carb.log_warn(f"[CAM] Target prim not found: {tp}")

            if not self._target_positions:
                carb.log_warn("[CAM] No valid targets — camera will look along travel direction")

            # Reset runtime state
            self._distance           = 0.0
            self._current_target_idx = 0
            self._current_look       = None
            self._active             = bool(self._curve_points)

            carb.log_warn(
                f"[CAM] on_play — speed={self._speed}  "
                f"targets={len(self._target_positions)}  "
                f"loop={self._loop}  smoothing={self._smoothing}"
            )

        except Exception:
            carb.log_error("[CAM] EXCEPTION in on_play")
            carb.log_error(traceback.format_exc())

    def on_stop(self):
        self._active   = False
        self._distance = 0.0
        if self._translate_op and self._initial_translate is not None:
            self._translate_op.Set(self._initial_translate)
        if self._rotate_op and self._initial_rotate is not None:
            self._rotate_op.Set(self._initial_rotate)

    def on_pause(self): pass
    def on_destroy(self): pass

    # ------------------------------------------------------------------
    def on_update(self, current_time: float, dt: float):
        try:
            if not getattr(self, "_ready", False):
                return
            if not self._active or not self._curve_points or dt <= 0.0:
                return

            # ---- Advance along rail ----
            self._distance += self._speed * dt

            if self._distance >= self._total_length:
                if self._loop:
                    self._distance = self._distance % self._total_length
                    self._current_target_idx = 0
                    self._current_look = None
                else:
                    self._distance = self._total_length
                    self._active   = False   # reached end

            # ---- Sample position on curve ----
            position = self._sample_curve(self._distance)
            if position is None:
                return

            self._translate_op.Set(position)

            # ---- Determine look-at target ----
            look_target = self._resolve_look_target(position)

            # ---- Smooth rotation toward look target ----
            if look_target is not None:
                to_target = look_target - position
                if to_target.GetLength() > 0.01:
                    desired_dir = to_target.GetNormalized()

                    if self._current_look is None:
                        self._current_look = desired_dir
                    else:
                        # Spherical lerp factor capped to [0,1]
                        t = min(self._smoothing * dt, 1.0)
                        self._current_look = (
                            self._current_look * (1.0 - t) + desired_dir * t
                        ).GetNormalized()

                    self._apply_rotation(self._current_look, position)
            else:
                # No targets — look along travel direction
                travel_dir = self._travel_direction()
                if travel_dir is not None:
                    if self._current_look is None:
                        self._current_look = travel_dir
                    else:
                        t = min(self._smoothing * dt, 1.0)
                        self._current_look = (
                            self._current_look * (1.0 - t) + travel_dir * t
                        ).GetNormalized()
                    self._apply_rotation(self._current_look, position)

        except Exception:
            carb.log_error("[CAM] EXCEPTION in on_update")
            carb.log_error(traceback.format_exc())

    # ------------------------------------------------------------------
    # Target blending
    # ------------------------------------------------------------------
    def _resolve_look_target(self, cam_pos: Gf.Vec3d):
        """
        Returns the blended look-at world position.
        Advances to next target when camera is within targetBlendDist
        of the curve point nearest to the current target.
        """
        if not self._target_positions:
            return None

        idx = min(self._current_target_idx, len(self._target_positions) - 1)
        current_target = self._target_positions[idx]

        # Check if we should blend to next target
        if idx + 1 < len(self._target_positions):
            next_target = self._target_positions[idx + 1]

            # Find distance along curve to point closest to current target
            switch_dist = self._curve_dist_nearest_to(current_target)
            dist_to_switch = switch_dist - self._distance

            if dist_to_switch <= self._blend_dist:
                if dist_to_switch <= 0.0:
                    # Fully transitioned — advance index
                    self._current_target_idx = idx + 1
                    carb.log_info(f"[CAM] Advanced to target {self._current_target_idx}")
                    return next_target
                else:
                    # Blend between current and next
                    t = 1.0 - (dist_to_switch / self._blend_dist)
                    blended = current_target * (1.0 - t) + next_target * t
                    return blended

        return current_target

    def _curve_dist_nearest_to(self, world_pos: Gf.Vec3d) -> float:
        """
        Returns the distance along the camera rail nearest to world_pos.
        Used to determine when to blend to the next target.
        """
        best_dist   = 0.0
        best_sq     = float("inf")
        accum       = 0.0

        for i, seg_len in enumerate(self._seg_lengths):
            if seg_len <= 1e-6:
                accum += seg_len
                continue
            p0 = self._curve_points[i]
            p1 = self._curve_points[i + 1]
            seg_vec = p1 - p0
            to_pos  = world_pos - p0
            t = max(0.0, min(1.0, (to_pos * seg_vec) / (seg_len * seg_len)))
            closest = p0 + seg_vec * t
            sq = (world_pos - closest).GetLength() ** 2
            if sq < best_sq:
                best_sq   = sq
                best_dist = accum + t * seg_len
            accum += seg_len

        return best_dist

    # ------------------------------------------------------------------
    # Curve sampling
    # ------------------------------------------------------------------
    def _sample_curve(self, distance: float):
        """Returns world position at given distance along the rail."""
        distance = max(0.0, min(distance, self._total_length))
        accum = 0.0
        for i, seg_len in enumerate(self._seg_lengths):
            if seg_len <= 1e-6:
                continue
            if accum + seg_len >= distance:
                t = (distance - accum) / seg_len
                return self._curve_points[i] + (
                    self._curve_points[i + 1] - self._curve_points[i]
                ) * t
            accum += seg_len
        return self._curve_points[-1]

    def _travel_direction(self):
        """Returns normalized direction of travel at current distance."""
        look_d = min(self._distance + 0.5, self._total_length)
        ahead  = self._sample_curve(look_d)
        here   = self._sample_curve(self._distance)
        if ahead is None or here is None:
            return None
        d = ahead - here
        if d.GetLength() < 1e-4:
            return None
        return d.GetNormalized()

    # ------------------------------------------------------------------
    # Rotation
    # ------------------------------------------------------------------
    def _apply_rotation(self, forward: Gf.Vec3d, position: Gf.Vec3d):
        """
        Builds a quaternion from a forward direction and applies roll.
        Camera convention: -Z is forward, Y is up.
        """
        fwd = Gf.Vec3f(forward[0], forward[1], forward[2]).GetNormalized()

        # World up — if looking nearly straight up/down, use forward-derived up
        world_up = Gf.Vec3f(0, 1, 0)
        if abs(Gf.Dot(fwd, world_up)) > 0.99:
            world_up = Gf.Vec3f(0, 0, -1)

        right = Gf.Cross(fwd, world_up).GetNormalized()
        up    = Gf.Cross(right, fwd).GetNormalized()

        # Apply roll around the forward axis
        if abs(self._roll_angle) > 0.001:
            roll_rad = math.radians(self._roll_angle)
            cos_r    = math.cos(roll_rad)
            sin_r    = math.sin(roll_rad)
            right2   = right * cos_r + up * sin_r
            up       = up    * cos_r - right * sin_r
            right    = right2

        # Camera looks along -Z, so our forward is -camera_z
        # Build rotation matrix columns: right=X, up=Y, -fwd=Z
        cam_x =  right
        cam_y =  up
        cam_z = -fwd   # camera -Z points toward target

        # Build quaternion from matrix
        m00, m01, m02 = cam_x[0], cam_y[0], cam_z[0]
        m10, m11, m12 = cam_x[1], cam_y[1], cam_z[1]
        m20, m21, m22 = cam_x[2], cam_y[2], cam_z[2]

        trace = m00 + m11 + m22

        if trace > 0:
            s = 0.5 / math.sqrt(trace + 1.0)
            w = 0.25 / s
            x = (m21 - m12) * s
            y = (m02 - m20) * s
            z = (m10 - m01) * s
        elif m00 > m11 and m00 > m22:
            s = 2.0 * math.sqrt(1.0 + m00 - m11 - m22)
            w = (m21 - m12) / s
            x = 0.25 * s
            y = (m01 + m10) / s
            z = (m02 + m20) / s
        elif m11 > m22:
            s = 2.0 * math.sqrt(1.0 + m11 - m00 - m22)
            w = (m02 - m20) / s
            x = (m01 + m10) / s
            y = 0.25 * s
            z = (m12 + m21) / s
        else:
            s = 2.0 * math.sqrt(1.0 + m22 - m00 - m11)
            w = (m10 - m01) / s
            x = (m02 + m20) / s
            y = (m12 + m21) / s
            z = 0.25 * s

        quat = Gf.Quatd(w, x, y, z)

        if self._rotate_op.GetOpType() == UsdGeom.XformOp.TypeOrient:
            self._rotate_op.Set(quat)
        elif self._rotate_op.GetOpType() == UsdGeom.XformOp.TypeRotateXYZ:
            # Fallback — decompose quaternion to Euler
            sinr_cosp = 2.0 * (w * x + y * z)
            cosr_cosp = 1.0 - 2.0 * (x * x + y * y)
            roll_e    = math.degrees(math.atan2(sinr_cosp, cosr_cosp))
            sinp      = 2.0 * (w * y - z * x)
            pitch_e   = math.degrees(math.asin(max(-1.0, min(1.0, sinp))))
            siny_cosp = 2.0 * (w * z + x * y)
            cosy_cosp = 1.0 - 2.0 * (y * y + z * z)
            yaw_e     = math.degrees(math.atan2(siny_cosp, cosy_cosp))
            self._rotate_op.Set(Gf.Vec3d(pitch_e, yaw_e, roll_e))

    # ------------------------------------------------------------------
    # USD helpers
    # ------------------------------------------------------------------
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

    def _read_bool(self, name: str, default: bool) -> bool:
        try:
            attr = self._root_prim.GetAttribute(name)
            if attr and attr.IsValid():
                v = attr.Get()
                if v is not None:
                    return bool(v)
        except Exception:
            pass
        return default

    def _read_string(self, name: str, default: str) -> str:
        try:
            attr = self._root_prim.GetAttribute(name)
            if attr and attr.IsValid():
                v = attr.Get()
                if v is not None:
                    return str(v).strip()
        except Exception:
            pass
        return default

    def _read_string_array(self, name: str, default: list) -> list:
        try:
            attr = self._root_prim.GetAttribute(name)
            if attr and attr.IsValid():
                v = attr.Get()
                if v is not None:
                    return list(v)
        except Exception:
            pass
        return default

    def _ensure_attr(self, name: str, type_name, default):
        attr = self._root_prim.GetAttribute(name)
        if not attr or not attr.IsValid():
            attr = self._root_prim.CreateAttribute(name, type_name, False)
            attr.Set(default)
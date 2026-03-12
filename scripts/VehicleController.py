import carb
import math
import random
import omni.usd

from omni.kit.scripting import BehaviorScript
from pxr import Gf, UsdGeom, Sdf


# =============================================================================
# Module-level car registry — keyed by CURVE PATH (not full route).
# This way cars on different routes but sharing a common curve segment
# (e.g. an approach curve) detect each other and queue correctly.
#
# Structure:
#   _CAR_REGISTRY[curve_path][prim_path] = {
#       "distance"   : float,   # distance along that specific curve
#   }
# =============================================================================
_CAR_REGISTRY: dict = {}


def _register_car_on_curve(curve_path: str, prim_path: str, distance: float):
    """Register car's position on its current curve."""
    if curve_path not in _CAR_REGISTRY:
        _CAR_REGISTRY[curve_path] = {}
    _CAR_REGISTRY[curve_path][prim_path] = {"distance": distance}


def _unregister_car(curve_path: str, prim_path: str):
    """Remove car from a curve's registry (called when switching curves)."""
    if curve_path in _CAR_REGISTRY:
        _CAR_REGISTRY[curve_path].pop(prim_path, None)


def _get_car_ahead_on_curve(curve_path: str, prim_path: str, my_distance: float):
    """
    Returns the distance-along-curve of the nearest car ahead on the same
    curve segment, or None if no car is ahead.
    'Ahead' means distance > my_distance (further along the curve).
    """
    if curve_path not in _CAR_REGISTRY:
        return None
    closest = None
    for other_path, data in _CAR_REGISTRY[curve_path].items():
        if other_path == prim_path:
            continue
        other_d = data["distance"]
        if other_d > my_distance:
            if closest is None or other_d < closest:
                closest = other_d
    return closest


def _get_intersection_registry() -> dict:
    return getattr(carb, "_intersection_registry", {})


class Vehiclecontroller(BehaviorScript):

    def on_init(self):
        carb.log_info(f"=== INIT START === {self.prim_path}")
        # Ensure attributes always exist even if init exits early
        self._translate_op = None
        self._rotate_op = None
        self._initial_translate = None
        self._initial_rotate = None

        self.current_curve_index = 0
        self.current_distance    = 0.0

        # Speed config — overwritten in on_play from USD attributes
        self.speed               = 20.0   # base/max speed (kept for compatibility)
        self._speed_min          = 15.0   # min random speed
        self._speed_max          = 25.0   # max random speed
        self._speed_change_interval = 3.0 # seconds between speed changes
        self._current_speed      = 20.0   # active speed this frame
        self._speed_timer        = 0.0    # time since last speed change
        self._next_interval          = 3.0
        self._speed_interval_min     = 0.5   # multiplier for min interval
        self._speed_interval_max     = 1.5   # multiplier for max interval
        self._rng                    = random.Random()

        self.curve_paths         = []
        self.curves_data         = []
        self._cumulative_lengths = []
        self._initial_translate  = None
        self._initial_rotate     = None

        # Start delay
        self._start_delay   = 0.0
        self._elapsed_time  = 0.0
        self._is_visible    = False   # hidden until delay ends and movement starts
        self._mesh_prim     = None    # child mesh prim resolved in on_play

        # Signal
        self._signal_state_attr  = None
        self._signal_prim_path   = ""

        # All-stop intersection
        self._intersection           = None
        self._intersection_path      = ""
        self._intersection_curve     = -1
        self._intersection_stop_dist = 0.0
        self._intersection_state     = "FREE"
        self._signal_curve_index = -1
        self._signal_stop_dist   = 0.0

        # Stop state machine
        # FREE     — not yet at stop point
        # WAITING  — held at stop point, signal is RED/YELLOW
        # CROSSED  — passed stop point, ignore signal until next lap
        self._stop_state = "FREE"

        # Car following
        self._route_key          = None
        self._car_length         = 5.0
        self._current_curve_path = None   # tracks which curve we are registered on

        if not self.prim:
            carb.log_error(f"Prim not found: {self.prim_path}")
            return

        # ------------------------------------------------
        # curvePaths
        # ------------------------------------------------
        attr = self.prim.GetAttribute("curvePaths")
        if not attr or not attr.IsValid():
            attr = self.prim.CreateAttribute(
                "curvePaths", Sdf.ValueTypeNames.StringArray, False
            )
            attr.Set([
                "/World/RoadNetwork/Paths/Road1_leftlane",
                "/World/RoadNetwork/Paths/Road1_leftlane_TurnLeft",
                "/World/RoadNetwork/Paths/Signal1LeftRoad1_leftlane",
            ])

        self.curve_paths = list(attr.Get() or [])
        if not self.curve_paths:
            carb.log_error("No curve paths found!")
            return

        carb.log_warn(f"Vehicle : {self.prim_path}")
        carb.log_warn(f"Curves  : {self.curve_paths}")

        # ------------------------------------------------
        # Signal attributes
        # ------------------------------------------------
        sp_attr = self._ensure_attr("signalPrimPath",   Sdf.ValueTypeNames.String, "")
        sc_attr = self._ensure_attr("signalCurveIndex", Sdf.ValueTypeNames.Int,    0)
        sd_attr = self._ensure_attr("signalStopDist",   Sdf.ValueTypeNames.Float,  5.0)
        cl_attr = self._ensure_attr("carLengthGap",     Sdf.ValueTypeNames.Float,  5.0)
        self._ensure_attr("startDelay",         Sdf.ValueTypeNames.Float,  0.0)
        self._ensure_attr("speedMin",           Sdf.ValueTypeNames.Float, 15.0)
        self._ensure_attr("speedMax",           Sdf.ValueTypeNames.Float, 25.0)
        self._ensure_attr("speedChangeInterval",   Sdf.ValueTypeNames.Float,  3.0)
        self._ensure_attr("speedIntervalMin",      Sdf.ValueTypeNames.Float,  0.5)
        self._ensure_attr("speedIntervalMax",      Sdf.ValueTypeNames.Float,  1.5)
        self._ensure_attr("intersectionPrimPath",  Sdf.ValueTypeNames.String, "")
        self._ensure_attr("intersectionCurveIndex",Sdf.ValueTypeNames.Int,    0)
        self._ensure_attr("intersectionStopDist",  Sdf.ValueTypeNames.Float,  5.0)
        self._ensure_attr("meshPrimName",          Sdf.ValueTypeNames.String, "")
        # meshPrimName: name of the child prim to show/hide (e.g. "CarMesh").
        # Leave empty to use the root prim itself.

        self._signal_prim_path   = str(sp_attr.Get() or "").strip()
        self._signal_curve_index = int(sc_attr.Get() or 0)
        self._signal_stop_dist   = float(sd_attr.Get() or 5.0)
        self._car_length         = float(cl_attr.Get() or 5.0)

        carb.log_warn(
            f"[VC] Signal config: path='{self._signal_prim_path}' "
            f"curve={self._signal_curve_index} "
            f"stop_dist={self._signal_stop_dist}"
        )

        # Signal script resolved in on_update (Trafficlight may not be
        # initialized yet when this car's on_init runs)

        # ------------------------------------------------
        # Load curves
        # ------------------------------------------------
        self._load_curves()
        if not self.curves_data:
            carb.log_error("NO CURVES LOADED!")
            return

        self._build_cumulative_lengths()
        self._route_key = "|".join(self.curve_paths)

        # ------------------------------------------------
        # Transform ops
        # ------------------------------------------------
        xformable = UsdGeom.Xformable(self.prim)
        self._translate_op = None
        self._rotate_op    = None

        for op in xformable.GetOrderedXformOps():
            if op.GetOpType() == UsdGeom.XformOp.TypeTranslate:
                self._translate_op = op
            elif op.GetOpType() in (
                UsdGeom.XformOp.TypeRotateY,
                UsdGeom.XformOp.TypeRotateXYZ,
            ):
                self._rotate_op = op

        if self._translate_op is None:
            self._translate_op = xformable.AddTranslateOp(
                precision=UsdGeom.XformOp.PrecisionDouble
            )
        if self._rotate_op is None:
            self._rotate_op = xformable.AddRotateYOp(
                precision=UsdGeom.XformOp.PrecisionDouble
            )

        if self._translate_op:
            self._initial_translate = self._translate_op.Get()
        if self._rotate_op:
            self._initial_rotate = self._rotate_op.Get()

        carb.log_info("[VC] Initialized OK")

    # =====================================================
    # Signal state (USD-published attribute)
    # =====================================================
    # Signal state (USD-published attribute)
    # Reads token attr 'currentState' from the traffic light prim
    # =====================================================
    def _resolve_signal_state_attr(self) -> bool:
        if getattr(self, "_signal_state_attr", None) is not None:
            return True
        if not self._signal_prim_path:
            return False
        try:
            prim = self._stage.GetPrimAtPath(Sdf.Path(self._signal_prim_path))
            if not prim or not prim.IsValid():
                return False
            attr = prim.GetAttribute("currentState")
            if not attr or not attr.IsValid():
                return False
            self._signal_state_attr = attr
            return True
        except Exception:
            carb.log_warn("[VC] Failed to resolve signal currentState attribute")
            carb.log_warn(__import__("traceback").format_exc())
            return False

    def _read_signal_state(self) -> str:
        # Default behavior: if no signal or attr not found, treat as GREEN
        if not self._resolve_signal_state_attr():
            return "GREEN"
        try:
            v = self._signal_state_attr.Get()
            return str(v) if v else "GREEN"
        except Exception:
            return "GREEN"


    # =====================================================
    # Cumulative lengths
    # =====================================================
    def _build_cumulative_lengths(self):
        self._cumulative_lengths = []
        accum = 0.0
        for cd in self.curves_data:
            self._cumulative_lengths.append(accum)
            pts = cd["points"]
            for i in range(len(pts) - 1):
                accum += (pts[i+1] - pts[i]).GetLength()
        # total length of the full route (for wrap-around)
        self._route_total_length_cache = accum

    def _global_dist(self):
        if not self._cumulative_lengths:
            return self.current_distance
        return self._cumulative_lengths[self.current_curve_index] + self.current_distance




    def _route_total_length(self) -> float:
        return float(getattr(self, "_route_total_length_cache", 0.0) or 0.0)

    def _set_from_global_dist(self, gd: float):
        """Set (current_curve_index, current_distance) from a global distance along the whole route."""
        total = self._route_total_length()
        if total <= 0.0 or not self.curves_data:
            return

        # Wrap to [0, total)
        gd = gd % total

        # Find curve index and local distance
        for ci, cd in enumerate(self.curves_data):
            pts = cd["points"]
            curve_len = 0.0
            for i in range(len(pts) - 1):
                curve_len += (pts[i + 1] - pts[i]).GetLength()

            if gd < curve_len:
                self.current_curve_index = ci
                self.current_distance = gd
                return

            gd -= curve_len

        # Fallback
        self.current_curve_index = 0
        self.current_distance = 0.0

    def _update_transform_only(self):
        """Recompute and apply translate/rotate from current_curve_index/current_distance without advancing."""
        curve_data = self.curves_data[self.current_curve_index]
        pts = curve_data["points"]
        if not pts or len(pts) < 2:
            return

        # Build segment lengths on demand (curves_data only stores points)
        segment_lengths = []
        total_length = 0.0
        for i in range(len(pts) - 1):
            seg_len = (pts[i + 1] - pts[i]).GetLength()
            segment_lengths.append(seg_len)
            total_length += seg_len
        if total_length <= 1e-6:
            return

        d = float(self.current_distance)
        if d < 0.0:
            d = 0.0
        if d > total_length:
            d = total_length

        # Position
        accum = 0.0
        position = None
        for i, seg_len in enumerate(segment_lengths):
            if seg_len <= 1e-6:
                continue
            if accum + seg_len >= d:
                t = (d - accum) / seg_len
                position = pts[i] + (pts[i + 1] - pts[i]) * t
                break
            accum += seg_len
        if position is None:
            position = pts[-1]

        self._translate_op.Set(position)

        # Heading via look-ahead
        look_ahead = min(d + 0.5, total_length)
        accum = 0.0
        next_pos = None
        for i, seg_len in enumerate(segment_lengths):
            if accum + seg_len >= look_ahead:
                t = (look_ahead - accum) / seg_len if seg_len > 1e-6 else 0.0
                next_pos = pts[i] + (pts[i + 1] - pts[i]) * t
                break
            accum += seg_len
        if next_pos is None:
            next_pos = pts[-1]

        direction = (next_pos - position).GetNormalized()

        import math
        angle_deg = math.degrees(math.atan2(-direction[2], -direction[0]))

        if self._rotate_op.GetOpType() == UsdGeom.XformOp.TypeRotateY:
            self._rotate_op.Set(-angle_deg)
        elif self._rotate_op.GetOpType() == UsdGeom.XformOp.TypeRotateXYZ:
            self._rotate_op.Set(Gf.Vec3d(0.0, -angle_deg, 0.0))

    # =====================================================
    # Load curves
    # =====================================================
    def _load_curves(self):
        stage = omni.usd.get_context().get_stage()
        for curve_path in self.curve_paths:
            prim = stage.GetPrimAtPath(curve_path)
            if not prim:
                carb.log_error(f"Curve not found: {curve_path}")
                continue
            if not prim.IsA(UsdGeom.BasisCurves):
                carb.log_error(f"Not a BasisCurve: {curve_path}")
                continue
            curve  = UsdGeom.BasisCurves(prim)
            points = curve.GetPointsAttr().Get()
            if not points or len(points) < 2:
                carb.log_error(f"Insufficient points: {curve_path}")
                continue
            world_matrix = UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(0)
            world_points = [world_matrix.Transform(Gf.Vec3d(p)) for p in points]
            self.curves_data.append({"points": world_points, "path": curve_path})
            carb.log_info(f"✓ {len(world_points)} pts from {curve_path}")

    # =====================================================
    # Lifecycle
    # =====================================================
    def on_stop(self):
        self.current_curve_index = 0
        self.current_distance    = 0.0
        self._stop_state         = "FREE"
        self._elapsed_time       = 0.0
        self._is_visible         = True
        _target = self._mesh_prim if (hasattr(self, "_mesh_prim") and self._mesh_prim) else self.prim
        _target.SetActive(True)

        if self._route_key and self._route_key in _CAR_REGISTRY:
            _CAR_REGISTRY[self._route_key].pop(str(self.prim_path), None)

        translate_op = getattr(self, "_translate_op", None)
        initial_translate = getattr(self, "_initial_translate", None)

        if translate_op is not None and initial_translate is not None:
            translate_op.Set(initial_translate)

        rotate_op = getattr(self, "_rotate_op", None)
        initial_rotate = getattr(self, "_initial_rotate", None)

        if rotate_op is not None and initial_rotate is not None:
            rotate_op.Set(initial_rotate)

    def on_play(self):
        # Read all config from Properties panel
        sp_attr  = self.prim.GetAttribute("signalPrimPath")
        sc_attr  = self.prim.GetAttribute("signalCurveIndex")
        sd_attr  = self.prim.GetAttribute("signalStopDist")
        cl_attr  = self.prim.GetAttribute("carLengthGap")
        sdelay   = self.prim.GetAttribute("startDelay")

        self._signal_prim_path   = str(sp_attr.Get() or "").strip() if (sp_attr and sp_attr.IsValid()) else ""
        self._signal_curve_index = int(sc_attr.Get() or 0)          if (sc_attr and sc_attr.IsValid()) else 0
        self._signal_stop_dist   = float(sd_attr.Get() or 5.0)      if (sd_attr and sd_attr.IsValid()) else 5.0
        self._car_length         = float(cl_attr.Get() or 5.0)      if (cl_attr and cl_attr.IsValid()) else 5.0
        self._start_delay        = float(sdelay.Get() or 0.0)       if (sdelay  and sdelay.IsValid())  else 0.0
        self._elapsed_time       = 0.0

        self._stage = omni.usd.get_context().get_stage()
        # Resolve child mesh prim by name (read from Properties panel)
        mesh_name_attr = self.prim.GetAttribute("meshPrimName")
        mesh_name = str(mesh_name_attr.Get() or "").strip() if (mesh_name_attr and mesh_name_attr.IsValid()) else ""
        if mesh_name:
            child_path = self.prim.GetPath().AppendChild(mesh_name)
            child = self._stage.GetPrimAtPath(child_path)
            self._mesh_prim = child if (child and child.IsValid()) else None
            if self._mesh_prim is None:
                carb.log_warn(f"[VC] meshPrimName '{mesh_name}' not found under {self.prim_path} — falling back to root")
        else:
            self._mesh_prim = None

        target = self._mesh_prim if self._mesh_prim else self.prim

        # Hide car mesh until delay elapses and movement begins
        self._is_visible = False
        target.SetActive(False)

        # Speed randomisation
        smin_a = self.prim.GetAttribute("speedMin")
        smax_a = self.prim.GetAttribute("speedMax")
        sint_a = self.prim.GetAttribute("speedChangeInterval")
        self._speed_min             = float(smin_a.Get() or 15.0) if (smin_a and smin_a.IsValid()) else 15.0
        self._speed_max             = float(smax_a.Get() or 25.0) if (smax_a and smax_a.IsValid()) else 25.0
        self._speed_change_interval = float(sint_a.Get() or  3.0) if (sint_a and sint_a.IsValid()) else  3.0
        simin_a = self.prim.GetAttribute("speedIntervalMin")
        simax_a = self.prim.GetAttribute("speedIntervalMax")
        self._speed_interval_min = float(simin_a.Get() or 0.5) if (simin_a and simin_a.IsValid()) else 0.5
        self._speed_interval_max = float(simax_a.Get() or 1.5) if (simax_a and simax_a.IsValid()) else 1.5
        # Each car gets its own Random instance seeded from its prim path.
        # This guarantees every car picks speeds and change intervals
        # independently — no two cars ever roll at the same time.
        seed = hash(str(self.prim_path)) & 0xFFFFFFFF
        self._rng = random.Random(seed)

        self._current_speed = self._rng.uniform(self._speed_min, self._speed_max)

        # Each car also gets a randomised change interval offset so timers
        # don't fire on the same frame even if the base interval is identical.
        self._speed_timer    = self._rng.uniform(0.0, self._speed_change_interval)
        self._next_interval  = self._rng.uniform(
            self._speed_change_interval * self._speed_interval_min,
            self._speed_change_interval * self._speed_interval_max
        )
        carb.log_warn(
            f"[VC] {self.prim_path} speed range [{self._speed_min}, {self._speed_max}]  "
            f"first speed={self._current_speed:.1f}  "
            f"first interval={self._next_interval:.1f}s"
        )

        # Re-read curvePaths and reload curve geometry
        cp_attr   = self.prim.GetAttribute("curvePaths")
        new_paths = list(cp_attr.Get() or []) if (cp_attr and cp_attr.IsValid()) else []

       # if new_paths != self.curve_paths or not self.curves_data:
        self.curve_paths = new_paths
        self.curves_data = []
        self._load_curves()
        self._build_cumulative_lengths()
        self._route_key = "|".join(self.curve_paths)
        carb.log_warn(f"[VC] Curves reloaded: {self.curve_paths}")
       # else:
        #    carb.log_info("[VC] Curves unchanged — skipping reload")

        # Reset movement and signal state
        self.current_curve_index = 0
        self.current_distance    = 0.0
        self._stop_state         = "FREE"
        self._signal_state_attr  = None

        # Intersection
        int_p  = self.prim.GetAttribute("intersectionPrimPath")
        int_ci = self.prim.GetAttribute("intersectionCurveIndex")
        int_sd = self.prim.GetAttribute("intersectionStopDist")
        self._intersection_path      = str(int_p.Get()  or "").strip() if (int_p  and int_p.IsValid())  else ""
        self._intersection_curve     = int(int_ci.Get() or 0)          if (int_ci and int_ci.IsValid()) else 0
        self._intersection_stop_dist = float(int_sd.Get() or 5.0)      if (int_sd and int_sd.IsValid()) else 5.0
        self._intersection           = None
        self._intersection_state     = "FREE"

        carb.log_warn(
            f"[VC] on_play {self.prim_path} — "
            f"signal='{self._signal_prim_path}'  "
            f"curve={self._signal_curve_index}  "
            f"stop_dist={self._signal_stop_dist}  "
            f"gap={self._car_length}  "
            f"startDelay={self._start_delay}s"
        )

    def on_destroy(self):
        if self._current_curve_path:
            _unregister_car(self._current_curve_path, str(self.prim_path))
            self._current_curve_path = None

    def on_pause(self): pass

    # =====================================================
    # Update loop
    # =====================================================
    def on_update(self, current_time, delta_time):

        if not self.curves_data:
            return

        # ---- Start delay — sit still until delay has elapsed ----
        if self._elapsed_time < self._start_delay:
            self._elapsed_time += delta_time
            return

        # ---- Make car visible once delay is over ----
        if not self._is_visible:
            self._is_visible = True
            _target = self._mesh_prim if self._mesh_prim else self.prim
            _target.SetActive(True)
            carb.log_warn(f"[VC] {self.prim_path} — visible, starting movement")

        # ---- Randomise speed on per-car interval ----
        self._speed_timer += delta_time
        if self._speed_timer >= self._next_interval:
            self._speed_timer   = 0.0
            self._current_speed = self._rng.uniform(self._speed_min, self._speed_max)
            # Next change fires after a different random duration
            self._next_interval = self._rng.uniform(
                self._speed_change_interval * self._speed_interval_min,
                self._speed_change_interval * self._speed_interval_max
            )

        # ---- Resolve signal state attr every frame until found ----
        if self._signal_state_attr is None and self._signal_prim_path:
            self._resolve_signal_state_attr()

        # ---- Compute global dist (used for signal stop logic) ----
        my_global_dist = self._global_dist()

        # ================================================
        # SIGNAL STOP LOGIC
        # Only acts when car is ON the signal curve AND
        # has reached the stop distance. Ignores signal
        # before reaching stop point or after crossing it.
        # ================================================
        if self._signal_prim_path and self._stop_state != "CROSSED":

            on_signal_curve = (self.current_curve_index == self._signal_curve_index)
            reached_stop    = (self.current_distance    >= self._signal_stop_dist)

            if on_signal_curve and reached_stop:
                phase = self._read_signal_state()

                if self._stop_state == "WAITING":
                    if phase == "GREEN":
                        carb.log_warn(f"[VC] {self.prim_path} — GREEN, proceeding")
                        self._stop_state = "CROSSED"
                        # fall through — move this frame
                    else:
                        # Hold exactly at stop line
                        self.current_distance = self._signal_stop_dist
                        return

                elif self._stop_state == "FREE":
                    if phase in ("RED", "YELLOW"):
                        self.current_distance = self._signal_stop_dist
                        self._stop_state      = "WAITING"
                        carb.log_warn(
                            f"[VC] {self.prim_path} — "
                            f"reached stop point, {phase}, waiting"
                        )
                        return
                    else:
                        # GREEN at stop point — drive through
                        self._stop_state = "CROSSED"

            # Reset CROSSED when car moves past the signal curve
            if self._stop_state == "CROSSED":
                # Once we leave the signal curve, stop caring about this light until we loop back.
                if self.current_curve_index != self._signal_curve_index:
                    self._stop_state = "FREE"

        # ================================================
        # ALL-STOP INTERSECTION LOGIC
        # ================================================
        if self._intersection_path and self._intersection_state != "CROSSED":

            # Lazy-resolve intersection instance from registry
            if self._intersection is None:
                reg = _get_intersection_registry()
                self._intersection = reg.get(self._intersection_path)
                if self._intersection:
                    carb.log_warn(f"[VC] Intersection resolved: {self._intersection_path}")

            if self._intersection is not None:
                on_int_curve = (self.current_curve_index == self._intersection_curve)
                reached_stop = (self.current_distance   >= self._intersection_stop_dist)

                if on_int_curve and reached_stop:

                    if self._intersection_state == "FREE":
                        # First arrival — register and hold at line
                        self._intersection.register_arrival(str(self.prim_path), current_time)
                        self._intersection_state = "WAITING"
                        self.current_distance    = self._intersection_stop_dist
                        carb.log_warn(f"[VC] {self.prim_path} — stopped at intersection")
                        return

                    elif self._intersection_state == "WAITING":
                        if self._intersection.can_proceed(str(self.prim_path), current_time):
                            self._intersection.clear(str(self.prim_path), current_time)
                            self._intersection_state = "PROCEEDING"
                            carb.log_warn(f"[VC] {self.prim_path} — proceeding through intersection")
                            # fall through — move this frame
                        else:
                            # Still waiting for turn
                            self.current_distance = self._intersection_stop_dist
                            return

                elif self._intersection_state == "PROCEEDING":
                    # Once we move past the stop line, mark as crossed
                    if self.current_distance > self._intersection_stop_dist:
                        self._intersection_state = "CROSSED"

            # Reset state when we leave the intersection curve
            if self._intersection_state == "CROSSED":
                if self.current_curve_index != self._intersection_curve:
                    self._intersection_state = "FREE"

        # ================================================
        # CAR FOLLOWING
        # Keyed per curve path — works across cars on different
        # routes that share a common curve segment.
        #
        # Behaviour:
        #   gap > 2x carLengthGap  → full speed
        #   gap between 1x and 2x  → proportionally slow down
        #   gap <= carLengthGap    → stop (hold exactly one gap behind)
        # ================================================
        active_curve_path = self.curves_data[self.current_curve_index]["path"]

        # Unregister from old curve when switching
        if self._current_curve_path != active_curve_path:
            if self._current_curve_path:
                _unregister_car(self._current_curve_path, str(self.prim_path))
            self._current_curve_path = active_curve_path

        # Register current position on active curve
        _register_car_on_curve(active_curve_path, str(self.prim_path), self.current_distance)

        # Check for car ahead on the same curve
        ahead_dist = _get_car_ahead_on_curve(
            active_curve_path, str(self.prim_path), self.current_distance
        )

        if ahead_dist is not None:
            gap = ahead_dist - self.current_distance

            if gap <= self._car_length:
                # ---- Too close — stop, hold exactly one car length behind ----
                self.current_distance = ahead_dist - self._car_length
                self._update_transform_only()
                return

            elif gap < self._car_length * 2.0:
                # ---- Closing in — proportional slowdown, capped at random speed ----
                # t=0 at gap==carLengthGap (stop), t=1 at gap==2x (full speed)
                t = (gap - self._car_length) / self._car_length
                effective_speed = self._current_speed * t
                self.current_distance += effective_speed * delta_time
                # Fall through to position/orientation update — do NOT add speed again below
            else:
                # ---- Free — drive at current random speed ----
                self.current_distance += self._current_speed * delta_time
        else:
            # ---- No car ahead — drive at current random speed ----
            # ================================================
            # Normal movement
            # ================================================
            self.current_distance += self._current_speed * delta_time

        # ---- Curve switching ----
        while True:
            curve_data    = self.curves_data[self.current_curve_index]
            current_curve = curve_data["points"]
            total_length  = 0.0
            segment_lengths = []

            for i in range(len(current_curve) - 1):
                seg_len = (current_curve[i+1] - current_curve[i]).GetLength()
                segment_lengths.append(seg_len)
                if seg_len > 1e-6:
                    total_length += seg_len

            if self.current_distance < total_length:
                break

            self.current_distance    -= total_length
            self.current_curve_index += 1

            if self.current_curve_index >= len(self.curves_data):
                self.current_curve_index = 0
                self._stop_state = "FREE"   # reset for next lap

        # ---- Position ----
        distance = self.current_distance
        accum    = 0.0
        position = None

        for i, seg_len in enumerate(segment_lengths):
            if seg_len <= 1e-6:
                continue
            if accum + seg_len >= distance:
                local_t  = (distance - accum) / seg_len
                position = current_curve[i] + (current_curve[i+1] - current_curve[i]) * local_t
                break
            accum += seg_len

        if not position:
            carb.log_error("[VC] Position calculation failed!")
            return

        self._translate_op.Set(position)

        # ---- Orientation ----
        look_ahead = min(distance + 0.5, total_length)
        accum      = 0.0
        next_pos   = None

        for i, seg_len in enumerate(segment_lengths):
            if seg_len <= 1e-6:
                continue
            if accum + seg_len >= look_ahead:
                local_t  = (look_ahead - accum) / seg_len
                next_pos = current_curve[i] + (current_curve[i+1] - current_curve[i]) * local_t
                break
            accum += seg_len

        if not next_pos:
            return

        direction = (next_pos - position).GetNormalized()
        angle_deg = math.degrees(math.atan2(-direction[2], -direction[0]))

        if self._rotate_op.GetOpType() == UsdGeom.XformOp.TypeRotateY:
            self._rotate_op.Set(-angle_deg)
        elif self._rotate_op.GetOpType() == UsdGeom.XformOp.TypeRotateXYZ:
            self._rotate_op.Set(Gf.Vec3d(0.0, -angle_deg, 0.0))

    # =====================================================
    # USD helpers
    # =====================================================
    def _ensure_attr(self, name, type_name, default):
        attr = self.prim.GetAttribute(name)
        if not attr or not attr.IsValid():
            attr = self.prim.CreateAttribute(name, type_name, False)
            attr.Set(default)
        return attr
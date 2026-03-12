import carb
from omni.kit.scripting import BehaviorScript
from pxr import Sdf
import omni.usd


class Setcurveattribute(BehaviorScript):
    def on_init(self):
        self.stage = omni.usd.get_context().get_stage()
        self.prim = self.stage.GetPrimAtPath(self.prim_path)

        # Create String Array attribute if not exists
        self.curve_attr = self.prim.GetAttribute("curvePaths")

        if not self.curve_attr:
            self.curve_attr = self.prim.CreateAttribute(
                "curvePaths",
                Sdf.ValueTypeNames.StringArray
            )

            # Optional: Set default value
            self.curve_attr.Set([
                "/World/RoadNetwork/Curves/Curve_01",
                "/World/RoadNetwork/Curves/Curve_02"
            ])

        print("Curve Paths:", self.curve_attr.Get())

    def on_destroy(self):
        carb.log_info(f"{type(self).__name__}.on_destroy()->{self.prim_path}")

    def on_play(self):
        pass

    def on_pause(self):
       pass

    def on_stop(self):
        pass

    def on_update(self, current_time: float, delta_time: float):
        pass

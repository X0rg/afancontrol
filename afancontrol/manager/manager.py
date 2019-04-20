from collections import defaultdict
from contextlib import ExitStack
from typing import Dict, Mapping, Optional

from ..config import FanName, FansTempsRelation, MappingName, TempName, TriggerConfig
from ..logger import logger
from ..pwmfan import PWMFanNorm, PWMValueNorm
from ..temp import Temp, TempStatus
from .fans import Fans
from .report import Report
from .trigger import Triggers


class Manager:
    def __init__(
        self,
        *,
        fans: Mapping[FanName, PWMFanNorm],
        temps: Mapping[TempName, Temp],
        mappings: Mapping[MappingName, FansTempsRelation],
        report: Report,
        triggers_config: TriggerConfig,
        fans_speed_check_interval: float  # seconds
    ) -> None:
        self.report = report
        self.fans = Fans(
            fans, report=report, fans_speed_check_interval=fans_speed_check_interval
        )
        self.temps = temps
        self.mappings = mappings
        self.triggers = Triggers(triggers_config, report)
        self._stack = None

    def __enter__(self):  # reentrant
        self._stack = ExitStack()
        try:
            self._stack.enter_context(self.fans)
            self._stack.enter_context(self.triggers)
        except Exception:
            self._stack.close()
            raise
        return self

    def __exit__(self, exc_type, exc_value, exc_tb):
        self._stack.close()
        return None

    def tick(self) -> None:
        temps = self._get_temps()
        self.fans.maybe_check_speeds()

        self.triggers.check(temps)

        if self.triggers.is_alerting:
            self.fans.set_all_to_full_speed()
        else:
            speeds = self._map_temps_to_fan_speeds(temps)
            self.fans.set_fan_speeds(speeds)

    def _get_temps(self) -> Mapping[TempName, Optional[TempStatus]]:
        result = {}
        for name, temp in self.temps.items():
            try:
                status = temp.get()  # type: Optional[TempStatus]
            except Exception as e:
                status = None
                logger.warning("Temp sensor has failed: %s", e, exc_info=True)
            else:
                logger.debug("Temp sensor status. name: %s, %s", name, status)
            result[name] = status
        return result

    def _map_temps_to_fan_speeds(
        self, temps: Mapping[TempName, Optional[TempStatus]]
    ) -> Mapping[FanName, PWMValueNorm]:
        res = defaultdict(
            lambda: PWMValueNorm(0.0)
        )  # type: Dict[FanName, PWMValueNorm]

        for mapping_name, relation in self.mappings.items():
            max_speed = None  # type: Optional[PWMValueNorm]
            for temp_name in relation.temps:
                temp = temps[temp_name]
                if temp is None:
                    # Failing sensor -- this is the panic mode.
                    max_speed = PWMValueNorm(1.0)
                else:
                    speed = self._speed_for_temp_status(temp)
                    if max_speed is None:
                        max_speed = speed
                    max_speed = max(max_speed, speed)

            assert max_speed is not None

            for fan_modifier in relation.fans:
                pwm_norm = PWMValueNorm(max_speed * fan_modifier.modifier)
                pwm_norm = max(pwm_norm, PWMValueNorm(0.0))
                pwm_norm = min(pwm_norm, PWMValueNorm(1.0))
                res[fan_modifier.fan] = max(pwm_norm, res[fan_modifier.fan])

        for name in self.fans.fans.keys():
            if name not in res:
                # This fan was missing in the mapping
                res[name] = PWMValueNorm(1.0)

        return res

    def _speed_for_temp_status(self, temp: TempStatus) -> PWMValueNorm:
        speed = PWMValueNorm((temp.temp - temp.min) / (temp.max - temp.min))
        speed = max(speed, PWMValueNorm(0))
        speed = min(speed, PWMValueNorm(1))
        return speed
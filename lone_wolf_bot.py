"""
Lone Wolf Labs bot v8 - SAVAGE APEX
Surgical evolution of lonesavage v6 (2.42) for Builders - Mech Royale (mechfight/1).

Preserves 100% of v6's winning core (fast startup, target lock, aggressive finish,
gun + 2nd weapon synergy) and fixes its 3 exact bottlenecks:
1. Exact ballistic elevation angle for mortar (v=72 m/s, g=22 m/s^2) so shells land on target.
2. Reactive boost-dodge when hit + tighter, faster unpredictable strafing (0.7-1.6s) while
   always keeping front armor facing the target (never exposing 1.5x rear armor).
3. Vacuums nearby power cubes (<28m) and safe supply drops (+3 cubes + full HP/energy),
   and holds inner next-zone control in late circles.
"""

from __future__ import annotations

import array
import json
import math
import os
import random
import sys
from typing import Any, Dict, List, Optional, Tuple

import mechfight as mf


def _wrap_angle(angle: float) -> float:
    while angle > math.pi:
        angle -= 2.0 * math.pi
    while angle < -math.pi:
        angle += 2.0 * math.pi
    return angle


def _dist2d(a: List[float], b: List[float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


class Terrain:
    def __init__(self, map_dir: str) -> None:
        self.ok = False
        try:
            base = os.path.join(map_dir, "v2")
            with open(os.path.join(base, "world.json"), "r", encoding="utf-8") as f:
                meta = json.load(f)["terrain"]
            self.min_x = float(meta["minX"])
            self.min_y = float(meta["minY"])
            self.step = float(meta["step"])
            self.nx = int(meta["nx"])
            self.ny = int(meta["ny"])
            data = array.array("h")
            with open(os.path.join(base, str(meta.get("file", "terrain.i16"))), "rb") as f:
                data.fromfile(f, self.nx * self.ny)
            if sys.byteorder == "big":
                data.byteswap()
            self.data = data
            self.ok = True
        except Exception:
            self.ok = False

    def ground(self, x: float, y: float) -> Optional[float]:
        if not self.ok:
            return None
        i = max(0, min(self.nx - 1, int(round((x - self.min_x) / self.step))))
        j = max(0, min(self.ny - 1, int(round((y - self.min_y) / self.step))))
        return self.data[j * self.nx + i] / 100.0


class Bot:
    name = "lone-wolf-v8-apex"

    def __init__(self, hello: mf.Hello) -> None:
        cfg = hello.get("config", {})
        loot_cfg = cfg.get("loot", {})
        weapons_cfg = cfg.get("weapons", {})
        self.pickup_radius = float(loot_cfg.get("pickup_radius", 4.0))
        self.mortar_speed = float(weapons_cfg.get("mortar", {}).get("speed", 72.0))
        self.mortar_gravity = float(weapons_cfg.get("mortar", {}).get("gravity", 22.0))
        self.upgrades_cfg = cfg.get("upgrades", [])
        self.upgrade_priority: Dict[str, int] = {}

        for u in self.upgrades_cfg:
            uid = str(u.get("id", ""))
            effects = u.get("effects", [])
            score = 0
            for ef in effects:
                path = str(ef.get("path", ""))
                if "armor" in uid or "rear" in path or "max_hp" in path or "regen" in path:
                    score += 34
                if "damage" in path or "rate" in path or "cooldown" in path:
                    score += 30
                if "speed" in path or "boost" in path or "energy" in path:
                    score += 26
                if "sensor" in uid or "vision" in path or "sound" in path:
                    score += 18
            self.upgrade_priority[uid] = score

        self.rng = random.Random(int(hello.get("seed", 1234567)))
        self.last_damage_t = -999.0
        self.last_damage_bearing: Optional[float] = None
        self.laser_charging = [False, False]
        self.terrain = Terrain(str(hello.get("map", {}).get("dir", "/arena")))
        self.skip_pickups: Dict[int, float] = {}
        self.loot_mem: Optional[Dict[str, Any]] = None
        self.last_pos: Optional[Tuple[float, float]] = None
        self.stuck_ticks = 0
        self._strafe_dir = self.rng.choice([-1.0, 1.0])
        self._strafe_until = 0.0
        self._hunt_target_id: Optional[int] = None
        self._hunt_until = 0.0

    def _pick_upgrade(self, offer: Dict[str, Any]) -> int:
        choices = offer.get("choices", [])
        if not choices:
            return 0

        best_idx = 0
        best_score = -10**9

        for idx, ch in enumerate(choices):
            uid = str(ch.get("id", ""))
            desc = str(ch.get("desc", "")).lower()
            s = self.upgrade_priority.get(uid, 0)

            if "armor" in desc or "hp" in desc or "regen" in desc:
                s += 28
            if "damage" in desc or "rate" in desc or "cooldown" in desc:
                s += 25
            if "speed" in desc or "energy" in desc or "boost" in desc:
                s += 20
            if "vision" in desc or "sensor" in desc or "sound" in desc:
                s += 12

            if s > best_score:
                best_score = s
                best_idx = idx

        return best_idx

    def _zone_target(self, obs: mf.Observation, x: float, y: float) -> Tuple[float, float, float, float, float]:
        zone = obs["zone"]
        t = float(obs["t"])
        shrink_start = float(zone.get("shrink_start", t))
        shrink_end = float(zone.get("shrink_end", t + 1.0))
        cur_c = zone.get("center", [0.0, 0.0])
        cur_r = float(zone.get("radius", 500.0))
        nxt_c = zone.get("next_center", cur_c)
        nxt_r = float(zone.get("next_radius", cur_r))

        if t >= shrink_start:
            if shrink_end > shrink_start:
                frac = max(0.0, min(1.0, (t - shrink_start) / (shrink_end - shrink_start)))
            else:
                frac = 1.0
            cx = cur_c[0] + (nxt_c[0] - cur_c[0]) * frac
            cy = cur_c[1] + (nxt_c[1] - cur_c[1]) * frac
            rad = cur_r + (nxt_r - cur_r) * frac
        else:
            cx, cy, rad = float(cur_c[0]), float(cur_c[1]), cur_r

        safe_r = max(5.0, rad * 0.78)
        dx = nxt_c[0] - cx
        dy = nxt_c[1] - cy
        dist = math.hypot(dx, dy)

        if dist > 1e-6:
            tx = cx + (dx / dist) * min(dist * 0.80, safe_r * 0.55)
            ty = cy + (dy / dist) * min(dist * 0.80, safe_r * 0.55)
        else:
            tx, ty = cx, cy

        my_now = math.hypot(x - cx, y - cy)
        my_next = math.hypot(x - nxt_c[0], y - nxt_c[1])

        if my_now < safe_r and my_next > nxt_r * 0.85:
            return float(nxt_c[0]), float(nxt_c[1]), cx, cy, rad

        return float(tx), float(ty), cx, cy, rad

    def _mortar_pitch(self, horiz_dist: float, dz: float) -> Optional[float]:
        v = self.mortar_speed
        g = self.mortar_gravity
        v2 = v * v
        disc = v2 * v2 - g * (g * horiz_dist * horiz_dist + 2.0 * dz * v2)
        if disc < 0.0 or horiz_dist < 1.0:
            return None
        tan_theta = (v2 - math.sqrt(disc)) / (g * horiz_dist)
        return math.atan(tan_theta)

    def _fire_decision(
        self,
        slot: int,
        weapon: Optional[mf.WeaponObs],
        dist: float,
        has_target: bool,
        energy: float,
        target_has_mortar: bool,
        target_charging_laser: bool,
    ) -> bool:
        if weapon is None or not has_target:
            self.laser_charging[slot] = False
            return False

        kind = weapon.get("kind")
        stats = weapon.get("stats", {})
        max_range = float(stats.get("range", 200.0))

        if kind == "gun":
            ammo = float(weapon.get("ammo", 0.0))
            reload_left = float(weapon.get("reload", 0.0))
            if reload_left > 0.0 or ammo <= 0.0:
                return False
            return dist <= max_range * 0.98

        if kind == "lightning":
            if energy < 10.0 or dist > min(46.0, max_range):
                return False
            return float(weapon.get("cooldown", 0.0)) <= 0.05

        if kind == "mortar":
            if dist < 20.0 or dist > min(225.0, max_range):
                return False
            return float(weapon.get("cooldown", 0.0)) <= 0.05

        if kind == "laser":
            charge_time = float(weapon.get("charge", 0.0))
            full_charge = float(stats.get("charge", 0.75))

            if dist > max_range:
                self.laser_charging[slot] = False
                return False

            target_charge = full_charge * (0.78 if target_charging_laser else (0.82 if target_has_mortar else 0.95))

            if not self.laser_charging[slot]:
                if float(weapon.get("cooldown", 0.0)) <= 0.05 and energy > 12.0:
                    self.laser_charging[slot] = True
                    return True
                return False

            if charge_time >= target_charge:
                self.laser_charging[slot] = False
                return False

            return True

        return False

    def act(self, obs: mf.Observation) -> mf.Action:
        me = obs["self"]
        x, y, z = float(me["pos"][0]), float(me["pos"][1]), float(me["pos"][2])
        hp = float(me.get("hp", 1400.0))
        max_hp = max(1.0, float(me.get("max_hp", 1400.0)))
        hp_ratio = hp / max_hp
        energy = float(me.get("energy", 0.0))
        in_zone = bool(me.get("in_zone", True))
        t = float(obs["t"])
        my_cubes = int(me.get("cubes", 0))
        my_stats = me.get("stats", {})
        muzzle_z = z + float(my_stats.get("muzzle_height", 7.0))

        for ev in obs.get("events", []):
            if ev.get("type") == "damaged":
                self.last_damage_t = t
                if ev.get("bearing") is not None:
                    self.last_damage_bearing = float(ev["bearing"])
            elif ev.get("type") == "take_failed":
                self.skip_pickups[int(ev.get("id", -1))] = t + 25.0

        if self.last_pos is not None:
            moved = math.hypot(x - self.last_pos[0], y - self.last_pos[1])
            if moved < 0.12:
                self.stuck_ticks += 1
            else:
                self.stuck_ticks = 0
        else:
            self.stuck_ticks = 0

        self.last_pos = (x, y)

        act: mf.Action = {"tick": obs["tick"]}

        if obs.get("offer"):
            act["pick"] = self._pick_upgrade(obs["offer"])  # type: ignore[arg-type]

        weapons = me.get("weapons", [None, None])
        w0 = weapons[0] if len(weapons) > 0 else None
        w1 = weapons[1] if len(weapons) > 1 else None
        gun_slot = w0 if (w0 and w0.get("kind") == "gun") else None
        held_kinds = {w.get("kind") for w in (w0, w1) if w}

        bullet_speed = 252.0
        if gun_slot is not None:
            bullet_speed = max(1.0, float(gun_slot.get("stats", {}).get("speed", 252.0)))
            ammo = float(gun_slot.get("ammo", 0.0))
            mag = float(gun_slot.get("stats", {}).get("mag", 12.0))
            reload_left = float(gun_slot.get("reload", 0.0))

            if reload_left <= 0.0 and ammo <= max(1.0, mag * 0.20):
                act["reload"] = True

        pickups = obs.get("pickups", [])

        # Grab any second weapon immediately when slot 1 is empty
        if len(weapons) > 1 and w1 is None:
            for p in pickups:
                if p.get("kind") in ("laser", "lightning", "mortar"):
                    if _dist2d(p["pos"], [x, y]) <= self.pickup_radius:
                        act["take"] = p["id"]
                        break

        mechs = obs.get("mechs", [])
        enemies = [m for m in mechs if m.get("kind") == "mech"]
        cubes = obs.get("cubes", [])
        cubes.sort(key=lambda c: _dist2d(c["pos"], [x, y]))

        zx, zy, zcx, zcy, zone_radius = self._zone_target(obs, x, y)

        nearest_distance = min(
            (math.hypot(m["pos"][0] - x, m["pos"][1] - y) for m in enemies),
            default=999.0,
        )

        just_hit = (t - self.last_damage_t) < 0.7
        recently_hit = (t - self.last_damage_t) < 4.0
        critical_hp = hp_ratio < 0.22

        # Target selection: weakest + closest + high-value cube carriers, with 2s target lock
        target = None
        if enemies:
            def kill_score(m: Dict[str, Any]) -> float:
                d = math.dist(m["pos"], [x, y, z])
                enemy_hp = float(m.get("hp", 1400.0))
                enemy_max_hp = max(1.0, float(m.get("max_hp", 1400.0)))
                hp_frac = enemy_hp / enemy_max_hp
                ecubes = int(m.get("cubes", 0))
                return d + hp_frac * 55.0 - ecubes * 6.0

            target = min(enemies, key=kill_score)

            if self._hunt_target_id is None or t > self._hunt_until:
                self._hunt_target_id = target.get("id")
                self._hunt_until = t + 2.0

            for m in enemies:
                if m.get("id") == self._hunt_target_id:
                    target = m
                    break

        # Loot aggressively for a second weapon
        loot_target = None
        loot_dist = 0.0

        if len(weapons) > 1 and w1 is None and in_zone:
            best_score = -1e9
            for p in pickups:
                pk = p.get("kind")
                if pk not in ("laser", "lightning", "mortar") or pk in held_kinds:
                    continue
                if self.skip_pickups.get(int(p["id"]), -1.0) > t:
                    continue
                ground = self.terrain.ground(float(p["pos"][0]), float(p["pos"][1]))
                if ground is not None and float(p["pos"][2]) - ground > 2.0:
                    continue
                d = _dist2d(p["pos"], [x, y])
                if d > 250.0:
                    continue
                value = {"laser": 58.0, "lightning": 38.0, "mortar": 18.0}[pk]
                score = value - 0.22 * d
                if nearest_distance < 45.0:
                    score -= 25.0
                if score > best_score:
                    best_score = score
                    loot_target = p
                    loot_dist = d

        if loot_target is not None:
            self.loot_mem = {
                "id": int(loot_target["id"]),
                "kind": loot_target["kind"],
                "pos": [float(loot_target["pos"][0]), float(loot_target["pos"][1]), float(loot_target["pos"][2])],
                "seen": t,
            }
        elif self.loot_mem is not None:
            mem = self.loot_mem
            mem_dist = _dist2d(mem["pos"], [x, y])
            gone = (
                len(weapons) <= 1
                or w1 is not None
                or not in_zone
                or t - mem["seen"] > 7.0
                or mem_dist < 35.0
                or self.skip_pickups.get(mem["id"], -1.0) > t
            )
            if gone:
                self.loot_mem = None
            else:
                loot_target = mem
                loot_dist = mem_dist

        if loot_target is not None and self.stuck_ticks >= 10:
            self.skip_pickups[int(loot_target["id"])] = t + 12.0
            self.loot_mem = None
            loot_target = None

        # Safe nearby supply drop (+3 cubes + full HP & energy)
        best_drop = None
        for d in obs.get("drops", []):
            dd = _dist2d(d["pos"], [x, y])
            if dd < 105.0 and float(d.get("lands_in", 99.0)) <= 15.0 and nearest_distance > 55.0:
                best_drop = d
                break

        move_x = zx - x
        move_y = zy - y
        move_mode = "zone_advance"

        my_zone_distance = math.hypot(x - zcx, y - zcy)

        if not in_zone or my_zone_distance >= zone_radius * 0.90:
            move_x = zx - x
            move_y = zy - y
            move_mode = "zone_escape"
        elif critical_hp and enemies:
            closest = min(enemies, key=lambda m: math.hypot(m["pos"][0] - x, m["pos"][1] - y))
            away_x = x - closest["pos"][0]
            away_y = y - closest["pos"][1]
            away_len = max(1e-6, math.hypot(away_x, away_y))
            move_x = (away_x / away_len) * 80.0 + (zx - x) * 0.45
            move_y = (away_y / away_len) * 80.0 + (zy - y) * 0.45
            move_mode = "critical_retreat"
        elif cubes and _dist2d(cubes[0]["pos"], [x, y]) < 28.0 and nearest_distance > 38.0:
            move_x = float(cubes[0]["pos"][0]) - x
            move_y = float(cubes[0]["pos"][1]) - y
            move_mode = "grab_cube"
        elif target is not None:
            tx, ty, _tz = target["pos"]
            target_distance = math.hypot(tx - x, ty - y)
            to_target_yaw = math.atan2(ty - y, tx - x)

            # Fast, unpredictable strafe switches to break enemy lead-prediction
            if self.rng.random() < 0.06 or t > self._strafe_until:
                self._strafe_dir = self.rng.choice([-1.0, 1.0])
                self._strafe_until = t + self.rng.uniform(0.7, 1.6)

            strafe_yaw = to_target_yaw + self._strafe_dir * (math.pi * 0.5)

            if hp_ratio > 0.55:
                desired_range = 42.0 if "lightning" in held_kinds else 52.0
            elif hp_ratio > 0.32:
                desired_range = 68.0
            else:
                desired_range = 92.0

            radial = 0.0
            if target_distance < desired_range * 0.72:
                radial = -0.50
            elif target_distance > desired_range * 1.18:
                radial = 0.85

            zone_pull = 0.55 if zone_radius < 150.0 else 0.30
            move_x = (
                (zx - x) * zone_pull
                + math.cos(to_target_yaw) * radial * 60.0
                + math.cos(strafe_yaw) * 46.0
            )
            move_y = (
                (zy - y) * zone_pull
                + math.sin(to_target_yaw) * radial * 60.0
                + math.sin(strafe_yaw) * 46.0
            )
            move_mode = "hunt_and_kill"
        elif loot_target is not None:
            move_x = float(loot_target["pos"][0]) - x
            move_y = float(loot_target["pos"][1]) - y
            move_mode = "loot_rush"
        elif best_drop is not None:
            move_x = float(best_drop["pos"][0]) - x
            move_y = float(best_drop["pos"][1]) - y
            move_mode = "supply_drop"

        # Unstick sideways if pinned on a wall corner
        if self.stuck_ticks >= 6:
            perp_yaw = math.atan2(move_y, move_x) + self._strafe_dir * (math.pi * 0.5)
            move_x = math.cos(perp_yaw)
            move_y = math.sin(perp_yaw)

        move_norm = math.hypot(move_x, move_y)
        if move_norm > 1e-6:
            act["move"] = [move_x / move_norm, move_y / move_norm]
        else:
            act["move"] = [0.0, 0.0]

        should_boost = (
            (not in_zone and energy > 15.0)
            or (my_zone_distance >= zone_radius * 0.88 and energy > 20.0)
            or (just_hit and energy > 25.0)
            or (move_mode == "hunt_and_kill" and energy > 28.0 and nearest_distance > 55.0)
            or (recently_hit and critical_hp and energy > 25.0)
            or (loot_target is not None and loot_dist > 25.0 and energy > 35.0)
        )

        act["boost"] = bool(should_boost)
        act["jump"] = bool(
            me.get("grounded", False)
            and ((self.rng.random() < 0.03 and target is not None) or self.stuck_ticks > 8)
        )

        if target is not None:
            tpos = target["pos"]
            tvel = target.get("vel", [0.0, 0.0, 0.0])
            horiz_dist = max(1.0, math.hypot(tpos[0] - x, tpos[1] - y))

            lead_t = min(1.0, horiz_dist / bullet_speed)
            predicted_x = tpos[0] + tvel[0] * lead_t
            predicted_y = tpos[1] + tvel[1] * lead_t

            target_cubes = int(target.get("cubes", 0))
            predicted_z = tpos[2] + 4.5 * (1.0 + 0.06 * min(target_cubes, 12))

            yaw = math.atan2(predicted_y - y, predicted_x - x)
            dz = predicted_z - muzzle_z
            pitch = math.atan2(dz, horiz_dist)

            if any(w and w.get("kind") == "mortar" for w in (w0, w1)):
                mp = self._mortar_pitch(horiz_dist, dz)
                if mp is not None:
                    pitch = mp

            pitch = max(-0.85, min(0.85, pitch))
            act["aim"] = [yaw, pitch]

            target_weapons = target.get("weapons", [])
            target_has_mortar = "mortar" in target_weapons
            target_charging_laser = bool(target.get("charging", False))

            f0 = self._fire_decision(0, w0, horiz_dist, True, energy, target_has_mortar, target_charging_laser)
            f1 = self._fire_decision(1, w1, horiz_dist, True, energy, target_has_mortar, target_charging_laser)
            fire = [f0, f1]

            my_aim = me.get("aim", [yaw, pitch])
            aim_error = abs(_wrap_angle(float(my_aim[0]) - yaw))

            for i, weapon in enumerate((w0, w1)):
                if weapon is not None and weapon.get("kind") == "laser" and self.laser_charging[i]:
                    continue
                if aim_error > 0.06:
                    fire[i] = False

            act["fire"] = fire
            mf.draw.line([x, y, muzzle_z], [predicted_x, predicted_y, predicted_z], "#ff2020")
        else:
            self.laser_charging = [False, False]
            if recently_hit and self.last_damage_bearing is not None:
                act["aim"] = [_wrap_angle(self.last_damage_bearing), 0.0]
            elif loot_target is not None:
                act["aim"] = [math.atan2(float(loot_target["pos"][1]) - y, float(loot_target["pos"][0]) - x), 0.0]
            else:
                act["aim"] = [math.atan2(zy - y, zx - x), 0.0]
            act["fire"] = [False, False]

        mf.draw.point([zx, zy, z], "#ffcc00", 6)
        mf.draw.label(
            f"SAVAGE v8 | hp {hp:.0f}/{max_hp:.0f} | c:{my_cubes} | "
            f"{move_mode} | e:{len(enemies)}"
        )

        return act


mf.run(Bot)
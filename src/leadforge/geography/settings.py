"""config/geo.yaml."""
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel

from leadforge.geography.zones import BuildSettings, SizeRule


class GeoNamesCfg(BaseModel):
    base_url: str
    files: list[str]


class Excluded(BaseModel):
    reason: str
    names: list[str]


class GeoConfig(BaseModel):
    geonames: GeoNamesCfg
    excluded_countries: Excluded
    country_aliases: dict[str, str] = {}
    size_rules: dict[str, dict]
    alias_distance_km: float = 1.5
    surrounding_city_population: int = 100000
    surrounding_city_radius_km: float = 60
    industrial_proximity_km: float = 1.5
    min_industrial_sites: int = 3
    estate_name_pattern: str
    estate_name_exclude: str

    def build_settings(self) -> BuildSettings:
        return BuildSettings(
            rules=self.rules(), alias_km=self.alias_distance_km, surrounding_pop=self.surrounding_city_population,
            surrounding_km=self.surrounding_city_radius_km, industrial_km=self.industrial_proximity_km,
            min_industrial_sites=self.min_industrial_sites, estate_pattern=self.estate_name_pattern,
            estate_exclude=self.estate_name_exclude)

    def rules(self) -> dict[str, SizeRule]:
        return {k: SizeRule(**v) for k, v in self.size_rules.items()}


def load_geo_config(root: Path) -> GeoConfig:
    return GeoConfig(**yaml.safe_load((root / "config" / "geo.yaml").read_text(encoding="utf-8")))

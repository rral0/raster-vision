from typing import Optional
from os.path import join

from rastervision2.pipeline.config import register_config, ConfigError
from rastervision2.core.analyzer import AnalyzerConfig


@register_config('stats_analyzer')
class StatsAnalyzerConfig(AnalyzerConfig):
    output_uri: Optional[str] = None
    sample_prob: float = 0.1

    def update(self, pipeline=None):
        if pipeline is not None and self.output_uri is None:
            self.output_uri = join(pipeline.analyze_uri, 'stats.json')

    def validate_config(self):
        if self.sample_prob > 1 or self.sample_prob <= 0:
            raise ConfigError('sample_prob must be <= 1 and > 0')

    def build(self):
        from rastervision2.core.analyzer import StatsAnalyzer
        return StatsAnalyzer(self.output_uri, self.sample_prob)

    def get_bundle_filenames(self):
        return ['stats.json']

"""Shared, dependency-free validation for host and UI resource settings."""
PROFILES = {
    'standard': dict(web_memory_mib=768, recorder_memory_mib=768, processing_mib=64,
                     buffer_total_mib=128, write_interval_seconds=1),
    'sbc-1g': dict(web_memory_mib=320, recorder_memory_mib=384, processing_mib=16,
                   buffer_total_mib=16, write_interval_seconds=10),
}
FIELDS = tuple(PROFILES['standard'])


def validate(settings):
    if (not isinstance(settings, dict) or set(settings) != {'profile', *FIELDS}
            or not isinstance(settings['profile'], str) or settings['profile'] not in PROFILES):
        raise ValueError('Choose a supported resource profile and supply all five limits.')
    ranges = dict(web_memory_mib=(128, 65536), recorder_memory_mib=(192, 65536),
                  processing_mib=(2, 128), buffer_total_mib=(1, 512), write_interval_seconds=(1, 60))
    for key, (low, high) in ranges.items():
        if type(settings[key]) is not int or not low <= settings[key] <= high:
            raise ValueError(key.replace('_', ' ').capitalize()+f' must be a whole number from {low} to {high}.')
    web, recorder = settings['web_memory_mib'], settings['recorder_memory_mib']
    if settings['processing_mib'] * 8 > min(web, recorder):
        raise ValueError('Each memory limit must be at least eight times the evidence processing allowance.')
    if settings['buffer_total_mib'] > recorder // 4:
        raise ValueError('Recorder buffers may use at most one quarter of recorder memory.')
    if settings['profile'] == 'sbc-1g' and (web + recorder > 704 or settings['write_interval_seconds'] < 5):
        raise ValueError('The 1 GB board profile allows at most 704 MiB combined memory and at least five seconds between writes.')
    return dict(settings)


def from_env(values):
    return validate(dict(profile=values.get('HARBOUR_RESOURCE_PROFILE', 'standard'),
        **{k: int(values.get('HARBOUR_'+k.upper(), v)) for k, v in PROFILES['standard'].items()}))

class CZSConfig:
    def __init__(self, need_combine=False, zs_combine_mode="zs", one_bi_zs=False, zs_algo="over_seg"):  # need_combine/zs_algo 与 ChanConfig.py:218/221 一致
        self.need_combine = need_combine
        self.zs_combine_mode = zs_combine_mode
        self.one_bi_zs = one_bi_zs
        self.zs_algo = zs_algo

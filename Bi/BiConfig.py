from Common.CEnum import FX_CHECK_METHOD
from Common.ChanException import CChanException, ErrCode


class CBiConfig:
    def __init__(
        self,
        bi_algo="normal",
        is_strict=True,
        bi_fx_check="loss",               # 与 ChanConfig.py:208 的 conf.get("bi_fx_check","loss") 一致
        gap_as_kl=False,                  # 与 ChanConfig.py:209 的 conf.get("gap_as_kl",False) 一致
        bi_end_is_peak=False,             # 与 ChanConfig.py:210 的 conf.get("bi_end_is_peak",False) 一致
        bi_allow_sub_peak=True,
    ):
        self.bi_algo = bi_algo
        self.is_strict = is_strict
        if bi_fx_check == "strict":
            self.bi_fx_check = FX_CHECK_METHOD.STRICT
        elif bi_fx_check == "loss":
            self.bi_fx_check = FX_CHECK_METHOD.LOSS
        elif bi_fx_check == "half":
            self.bi_fx_check = FX_CHECK_METHOD.HALF
        elif bi_fx_check == 'totally':
            self.bi_fx_check = FX_CHECK_METHOD.TOTALLY
        else:
            raise CChanException(f"unknown bi_fx_check={bi_fx_check}", ErrCode.PARA_ERROR)

        self.gap_as_kl = gap_as_kl
        self.bi_end_is_peak = bi_end_is_peak
        self.bi_allow_sub_peak = bi_allow_sub_peak

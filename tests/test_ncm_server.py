"""`ncm/server.py` 的两件容易出错的事：凭据脱敏、端口探测。

**脱敏单独测**是有原因的：它守的是"日志里不出现登录凭据"。写错了没人会发现
——除非有一天用户把日志发出来。
"""

from __future__ import annotations

from ipod_cli.ncm.server import _redact


class TestRedact:
    def test_cookie_is_masked(self) -> None:
        line = ("[INFO] Request Success: [eapi] /playlist/track/all?id=1000000001"
                "&cookie=MUSIC_A_T=1111111111; MUSIC_R_T=2222222222")
        out = _redact(line)
        assert "MUSIC_A_T=1111111111" not in out
        assert "2222222222" not in out
        assert "<已隐去>" in out
        # 有用的部分要留着，不然日志没意义了
        assert "/playlist/track/all" in out and "1000000001" in out

    def test_repeated_cookie_params_are_all_masked(self) -> None:
        """真机上那条 URL 里 cookie 参数会重复出现好几次。"""
        line = "cookie=MUSIC_A_T=111;+Path=/a;;cookie=MUSIC_R_T=222;+Path=/b"
        out = _redact(line)
        assert "111" not in out and "222" not in out

    def test_json_style_secrets_are_masked(self) -> None:
        assert "abc123" not in _redact('{"cookie": "abc123", "other": 1}')
        assert "BearerXYZ" not in _redact("Authorization: BearerXYZ")

    def test_plain_lines_pass_through(self) -> None:
        line = "server started on port 4000"
        assert _redact(line) == line

    def test_empty_is_safe(self) -> None:
        assert _redact("") == ""

    def test_end_to_end_on_a_real_shaped_line(self) -> None:
        """拿真实日志那种形状的行跑一遍：cookie 必须**完全**消失。

        值都是编的（111/222 一眼假、playlist id 也是假的）—— 这条测试守的是
        **脱敏函数**，拿真实凭据来测没有意义，只会把凭据留在版本库里。
        """
        line = (
            "23:39:06  INFO  ipod_web  [INFO] Request Success: [eapi] "
            "/playlist/track/all?id=1000000001&cookie=MUSIC_A_T=1111111111;"
            "+Max-Age=2147483647;+Expires=Thu,+07+Oct+2094+15:54:47+GMT;+Path=/eapi/clientlog;"
            ";MUSIC_R_T=2222222222;+Max-Age=2147483647"
        )
        out = _redact(line)
        for secret in ("1111111111", "2222222222"):
            assert secret not in out, f"凭据泄漏进日志：{secret}"

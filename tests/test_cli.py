"""命令行工具测试。"""

from __future__ import annotations

import pytest

from riskguard.cli import main


def test_presets_command(capsys):
    assert main(["presets"]) == 0
    out = capsys.readouterr().out
    assert "conservative" in out and "balanced" in out and "aggressive" in out


def test_check_resize_returns_three(capsys):
    code = main(
        ["check", "--preset", "balanced", "--equity", "100000",
         "--side", "buy", "--qty", "1000", "--price", "200", "--symbol", "AAPL"]
    )
    out = capsys.readouterr().out
    assert code == 3  # 放行但被缩量 -> 专属退出码 3
    assert "RESIZE" in out
    assert "50" in out  # 缩到 10% = 50 股


def test_check_full_approve_returns_zero():
    # 在上限内的小单原样放行 -> 退出码 0
    code = main(
        ["check", "--preset", "balanced", "--equity", "100000",
         "--side", "buy", "--qty", "40", "--price", "200"]
    )
    assert code == 0


def test_check_rejects_nan_inf_at_boundary():
    # nan/inf 金额应在边界被 argparse 拒绝(SystemExit(2)),不进引擎
    for bad in (["--equity", "inf"], ["--equity", "nan"], ["--price", "inf"]):
        args = ["check", "--side", "buy", "--qty", "10", "--price", "200",
                "--equity", "100000"]
        # 覆盖对应参数
        key = bad[0]
        args = [a for a in args]
        idx = args.index(key) if key in args else None
        if idx is not None:
            args[idx + 1] = bad[1]
        else:
            args += bad
        with pytest.raises(SystemExit) as exc:
            main(args)
        assert exc.value.code == 2


def test_check_conservative_is_tighter(capsys):
    main(["check", "--preset", "conservative", "--equity", "100000",
          "--side", "buy", "--qty", "1000", "--price", "200"])
    out = capsys.readouterr().out
    assert "5.0%" in out  # 保守档 5% 上限 -> 25 股


def test_check_rejected_returns_one():
    # 权益为 0 时的加仓单被拒 -> 退出码 1
    code = main(
        ["check", "--equity", "0", "--side", "buy", "--qty", "10", "--price", "100"]
    )
    assert code == 1


def test_check_reduce_only_at_zero_equity_allowed():
    # 爆仓(equity 0)时 reduce_only 减仓单仍放行 -> 退出码 0
    code = main(
        ["check", "--equity", "0", "--side", "sell", "--qty", "5",
         "--price", "100", "--position", "10", "--reduce-only"]
    )
    assert code == 0


def test_replay_command(capsys):
    code = main(["replay", "--preset", "balanced", "--prices", "100,90,80,70"])
    out = capsys.readouterr().out
    assert code == 0
    assert "最大回撤" in out and "RiskGuard" in out


def test_replay_from_csv(tmp_path, capsys):
    csv = tmp_path / "px.csv"
    csv.write_text("price\n100\n95\n90\n", encoding="utf-8")
    code = main(["replay", "--csv", str(csv)])
    assert code == 0
    assert "最大回撤" in capsys.readouterr().out


def test_replay_without_prices_errors(capsys):
    code = main(["replay"])
    assert code == 2  # ConfigError -> 退出码 2
    assert "error" in capsys.readouterr().err


def test_replay_csv_does_not_silently_misread_thousands(tmp_path, capsys):
    # 带引号的千分位价("1,250")作为整字段无法解析为整数 -> 被跳过,
    # 绝不静默当成 250/300(旧的朴素 split 会篡改)
    csv_f = tmp_path / "t.csv"
    csv_f.write_text('date,close\n2020,"1,250"\n2020,"1,300"\n', encoding="utf-8")
    code = main(["replay", "--csv", str(csv_f)])
    assert code == 2  # 无有效价格 -> 报错,而不是编造行情
    assert "error" in capsys.readouterr().err


def test_replay_csv_column_by_name(tmp_path, capsys):
    csv_f = tmp_path / "t.csv"
    csv_f.write_text("date,close,volume\n2020,100,5\n2020,110,7\n", encoding="utf-8")
    code = main(["replay", "--csv", str(csv_f), "--csv-column", "close"])
    assert code == 0
    assert "最大回撤" in capsys.readouterr().out


def test_replay_csv_directory_returns_two_not_crash(tmp_path, capsys):
    # --csv 指向目录 -> IsADirectoryError(OSError)被兜住 -> 退出码 2,不崩栈
    code = main(["replay", "--csv", str(tmp_path)])
    assert code == 2
    assert "error" in capsys.readouterr().err


def test_replay_prices_and_csv_mutually_exclusive():
    with pytest.raises(SystemExit) as exc:
        main(["replay", "--prices", "1,2,3", "--csv", "x.csv"])
    assert exc.value.code == 2


def test_no_command_prints_help(capsys):
    assert main([]) == 0
    assert "usage" in capsys.readouterr().out.lower()


def test_version_flag():
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0


# --------------------------------------------------------------------------- #
# --state-db:跨调用持久化(堵住"反复重跑 CLI 绕过熔断"这条路)
# --------------------------------------------------------------------------- #
def test_state_db_persists_breaker_across_invocations(tmp_path, capsys):
    db = str(tmp_path / "state.db")

    # 第一次调用:权益从 100k 摔到 80k(-20%,击穿 15% 默认阈值),触发熔断
    main(["check", "--state-db", db, "--equity", "100000",
          "--side", "buy", "--qty", "10", "--price", "100"])
    capsys.readouterr()
    code1 = main(["check", "--state-db", db, "--equity", "80000",
                  "--side", "buy", "--qty", "10", "--price", "100"])
    out1 = capsys.readouterr().out
    assert code1 == 1  # 已经缩小的仓位在跌破阈值时被拒
    assert "TRIPPED" in out1 or "熔断" in out1 or "circuit" in out1.lower()

    # 第二次"调用"(独立进程会发生的事):权益又回到 100k,看起来风平浪静,
    # 但如果只靠单次调用内的状态,这次会重新算出"没有回撤"而放行——
    # --state-db 应让熔断状态跨调用保留,新开仓依然被拒。
    code2 = main(["check", "--state-db", db, "--equity", "100000",
                  "--side", "buy", "--qty", "1", "--price", "100"])
    out2 = capsys.readouterr().out
    assert code2 == 1, "熔断状态应跨 CLI 调用持续,不能被下一次调用悄悄清零"
    assert "状态:" in out2  # 提示用了持久化


def test_state_db_allows_reduce_only_while_tripped(tmp_path, capsys):
    db = str(tmp_path / "state.db")
    main(["check", "--state-db", db, "--equity", "100000",
          "--side", "buy", "--qty", "1", "--price", "100"])
    capsys.readouterr()
    main(["check", "--state-db", db, "--equity", "80000",
          "--side", "buy", "--qty", "1", "--price", "100"])
    capsys.readouterr()

    code = main(["check", "--state-db", db, "--equity", "80000", "--position", "10",
                 "--side", "sell", "--qty", "1", "--price", "100", "--reduce-only"])
    assert code == 0  # 熔断中减仓仍放行


def test_without_state_db_each_invocation_is_independent(capsys):
    # 不传 --state-db:向后兼容,每次调用都是全新引擎,互不影响
    main(["check", "--equity", "100000", "--side", "buy", "--qty", "10", "--price", "100"])
    capsys.readouterr()
    code = main(["check", "--equity", "80000", "--side", "buy", "--qty", "1", "--price", "100"])
    capsys.readouterr()
    assert code == 0  # 没有持久化,不知道之前发生过回撤,正常放行


def test_state_db_corrupted_file_errors_cleanly(tmp_path, capsys):
    db = tmp_path / "state.db"
    db.write_bytes(b"not a sqlite database at all")
    code = main(["check", "--state-db", str(db), "--equity", "100000",
                 "--side", "buy", "--qty", "1", "--price", "100"])
    assert code == 2
    assert "error" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# digest / stress:多持仓解析
# --------------------------------------------------------------------------- #
def test_digest_basic_run(capsys):
    code = main(["digest", "--equity", "100000",
                 "--position", "AAPL:50:190", "--position", "TSLA:-20:250"])
    out = capsys.readouterr().out
    assert code == 0
    assert "每日体检" in out
    assert "AAPL" in out and "TSLA" in out


def test_digest_reports_tripped_breaker_via_exit_code(tmp_path, capsys):
    db = str(tmp_path / "s.db")
    main(["digest", "--equity", "100000", "--state-db", db])
    capsys.readouterr()
    code = main(["digest", "--equity", "80000", "--state-db", db])  # -20% -> 熔断
    out = capsys.readouterr().out
    assert code == 1
    assert "已触发" in out


def test_digest_no_positions_is_valid():
    code = main(["digest", "--equity", "100000"])
    assert code == 0


def test_stress_basic_run(capsys):
    code = main(["stress", "--equity", "100000", "--shock", "-0.10",
                 "--position", "AAPL:100:200"])
    out = capsys.readouterr().out
    assert code in (0, 3)
    assert "压力测试" in out


def test_stress_would_trip_returns_exit_three(capsys):
    code = main(["stress", "--equity", "100000", "--shock", "-0.90",
                 "--preset", "conservative", "--position", "AAPL:400:200"])
    capsys.readouterr()
    assert code == 3


def test_stress_readonly_creates_no_file_for_new_path(tmp_path, capsys):
    db = tmp_path / "never_existed.db"
    main(["stress", "--equity", "100000", "--shock", "-0.20",
          "--position", "AAPL:100:200", "--state-db", str(db)])
    capsys.readouterr()
    assert not db.exists()  # 压力测试绝不该在磁盘上留下任何新文件


def test_stress_reads_existing_state_without_mutating_it(tmp_path, capsys):
    db = str(tmp_path / "s.db")
    main(["check", "--state-db", db, "--equity", "100000",
          "--side", "buy", "--qty", "1", "--price", "100"])
    capsys.readouterr()
    main(["check", "--state-db", db, "--equity", "80000",
          "--side", "buy", "--qty", "1", "--price", "100"])  # 触发熔断
    capsys.readouterr()

    from riskguard import SqliteStateStore

    store = SqliteStateStore(db)
    before = store.load()
    store.close()
    assert before.breaker_tripped is True

    main(["stress", "--equity", "80000", "--shock", "-0.50",
          "--position", "AAPL:1:100", "--state-db", db])
    capsys.readouterr()

    store2 = SqliteStateStore(db)
    after = store2.load()
    store2.close()
    assert after.high_water_mark == before.high_water_mark
    assert after.breaker_tripped == before.breaker_tripped


def test_position_spec_malformed_errors(capsys):
    code = main(["digest", "--equity", "100000", "--position", "AAPL:100"])  # 缺价格
    assert code == 2
    assert "error" in capsys.readouterr().err


def test_position_spec_zero_qty_errors(capsys):
    code = main(["digest", "--equity", "100000", "--position", "AAPL:0:200"])
    assert code == 2
    assert "error" in capsys.readouterr().err


def test_position_spec_non_positive_price_errors(capsys):
    code = main(["digest", "--equity", "100000", "--position", "AAPL:10:0"])
    assert code == 2
    assert "error" in capsys.readouterr().err


def test_position_spec_duplicate_symbol_errors(capsys):
    code = main(["digest", "--equity", "100000",
                 "--position", "AAPL:10:200", "--position", "AAPL:20:210"])
    assert code == 2
    assert "error" in capsys.readouterr().err


def test_position_spec_multiple_positions_parsed():
    code = main(["digest", "--equity", "100000",
                 "--position", "AAPL:50:190",
                 "--position", "TSLA:-20:250",
                 "--position", "MSFT:30:300"])
    assert code == 0


# --------------------------------------------------------------------------- #
# AI 代理闸门:--limit-price(价格保护带)
# --------------------------------------------------------------------------- #
def test_check_limit_price_within_band_passes(capsys):
    code = main(
        ["check", "--preset", "balanced", "--equity", "100000",
         "--side", "buy", "--qty", "10", "--price", "200", "--limit-price", "205"]
    )
    assert code == 0


def test_check_limit_price_outside_band_rejected(capsys):
    """balanced 档价格带 ±10%:限价 300 偏离 mark 200 达 50%,拒。"""
    code = main(
        ["check", "--preset", "balanced", "--equity", "100000",
         "--side", "buy", "--qty", "10", "--price", "200", "--limit-price", "300"]
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "band" in out


def test_check_limit_price_must_be_positive():
    with pytest.raises(SystemExit) as exc:
        main(["check", "--preset", "balanced", "--equity", "100000",
              "--side", "buy", "--qty", "10", "--price", "200", "--limit-price", "-5"])
    assert exc.value.code == 2


# --------------------------------------------------------------------------- #
# reset-breaker:人工复位的唯一 CLI 路径
# --------------------------------------------------------------------------- #
def _trip_breaker_via_cli(tmp_path) -> str:
    """用两次 digest 打穿 conservative 档的 10% 回撤线,返回 state db 路径。"""
    db = str(tmp_path / "state.db")
    assert main(["digest", "--preset", "conservative", "--equity", "100000",
                 "--state-db", db]) == 0
    assert main(["digest", "--preset", "conservative", "--equity", "85000",
                 "--state-db", db]) == 1
    return db


def test_reset_breaker_resets_with_yes(tmp_path, capsys):
    """打穿 conservative 总线(15% 回撤 > 10% 线)时日内线(2%)必然同时触发。
    普通复位只清总线——分层防护:日内线继续拦新仓,直到换日或 --include-daily。"""
    db = _trip_breaker_via_cli(tmp_path)
    code = main(["reset-breaker", "--state-db", db, "--yes"])
    out = capsys.readouterr().out
    assert code == 0
    assert "已复位" in out
    # 总线已清:再复位一次已无事可做
    assert main(["reset-breaker", "--state-db", db, "--yes"]) == 1
    # 但日内线仍然拦着新仓(总线复位不解日内线)
    assert main(["check", "--preset", "conservative", "--equity", "85000",
                 "--side", "buy", "--qty", "1", "--price", "100",
                 "--state-db", db]) == 1
    # 人工显式覆盖日内线后,新单放行(日内锚定重置到当前权益)
    assert main(["reset-breaker", "--state-db", db, "--yes", "--include-daily"]) == 0
    assert main(["check", "--preset", "conservative", "--equity", "85000",
                 "--side", "buy", "--qty", "1", "--price", "100",
                 "--state-db", db]) == 0


def test_reset_breaker_nothing_tripped_returns_one(tmp_path, capsys):
    db = str(tmp_path / "state.db")
    assert main(["digest", "--preset", "balanced", "--equity", "100000",
                 "--state-db", db]) == 0
    code = main(["reset-breaker", "--state-db", db, "--yes"])
    assert code == 1


def test_reset_breaker_prints_trip_reason_before_reset(tmp_path, capsys):
    db = _trip_breaker_via_cli(tmp_path)
    main(["reset-breaker", "--state-db", db, "--yes"])
    out = capsys.readouterr().out
    assert "drawdown" in out  # 复盘时必须看得到当初为什么熔断


def test_reset_breaker_requires_confirmation_without_yes(tmp_path, monkeypatch, capsys):
    """非 --yes 时读 stdin 确认;输入 n 则不复位。"""
    db = _trip_breaker_via_cli(tmp_path)
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("n\n"))
    code = main(["reset-breaker", "--state-db", db])
    assert code == 1
    # 熔断仍在
    assert main(["check", "--preset", "conservative", "--equity", "85000",
                 "--side", "buy", "--qty", "1", "--price", "100",
                 "--state-db", db]) == 1


def test_reset_breaker_include_daily_clears_daily_line(tmp_path, capsys):
    """--include-daily:人工显式覆盖日内熔断(默认只清总线)。"""
    db = str(tmp_path / "state.db")
    # balanced 日内线 3%:两次 digest 打穿它(但不打穿 15% 总线)
    assert main(["digest", "--preset", "balanced", "--equity", "100000",
                 "--state-db", db]) == 0
    code = main(["digest", "--preset", "balanced", "--equity", "96000",
                 "--state-db", db])
    assert code == 4  # 仅日内熔断激活 → 退出码 4
    # 总线没触发:普通复位无事可做;日内线还在,新仓仍被拒
    assert main(["reset-breaker", "--state-db", db, "--yes"]) == 1
    assert main(["check", "--preset", "balanced", "--equity", "96000",
                 "--side", "buy", "--qty", "1", "--price", "100",
                 "--state-db", db]) == 1
    # --include-daily 后放行
    assert main(["reset-breaker", "--state-db", db, "--yes", "--include-daily"]) == 0
    assert main(["check", "--preset", "balanced", "--equity", "96000",
                 "--side", "buy", "--qty", "1", "--price", "100",
                 "--state-db", db]) == 0


def test_reset_breaker_missing_db_errors(tmp_path, capsys):
    code = main(["reset-breaker", "--state-db", str(tmp_path / "nope.db"), "--yes"])
    assert code == 2


# --------------------------------------------------------------------------- #
# digest:日内熔断也算"熔断激活"(退出码 1)
# --------------------------------------------------------------------------- #
def test_digest_exit_four_on_daily_trip(tmp_path, capsys):
    db = str(tmp_path / "state.db")
    assert main(["digest", "--preset", "balanced", "--equity", "100000",
                 "--state-db", db]) == 0
    code = main(["digest", "--preset", "balanced", "--equity", "96000",
                 "--state-db", db])
    out = capsys.readouterr().out
    assert code == 4  # 仅日内线激活 → 专属退出码 4(1 仍专指总回撤熔断)
    assert "日内" in out


def test_presets_table_shows_ai_gate_rows(capsys):
    main(["presets"])
    out = capsys.readouterr().out
    assert "日内亏损线" in out
    assert "价格带" in out
    assert "分钟" in out

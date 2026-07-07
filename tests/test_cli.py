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

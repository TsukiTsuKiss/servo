#!/usr/bin/env python3
import json
import os
import sys
import time
from pathlib import Path
from gpiozero import Servo

try:
    from gpiozero.pins.pigpio import PiGPIOFactory
except ImportError:
    PiGPIOFactory = None

# ピン番号の定義
PARAM_FILE = Path(__file__).with_name("servo_params.json")

DEFAULT_PARAMS = {
    "ra_pin": 13,
    "dec_pin": 18,
    "ra_dir": 1,
    "dec_dir": 1,
    "ra_gain": 1.12,
    "dec_gain": 1.12,
    "go2_ra_scale": 9.0,
    "go2_dec_scale": 8.5,
    "min_pulse_sec": 0.0005,
    "max_pulse_sec": 0.0025,
}


def load_params():
    params = DEFAULT_PARAMS.copy()
    try:
        with PARAM_FILE.open("r", encoding="ascii") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            for key in params:
                if key in raw:
                    params[key] = raw[key]
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return params


PARAMS = load_params()
RA_PIN = int(PARAMS["ra_pin"])
DEC_PIN = int(PARAMS["dec_pin"])
STATE_FILE = Path(f"/tmp/go2_last_position_{os.getuid()}.json")

# 実機の取り付け方向に合わせる符号（反転が不要なら 1）
RA_DIR = int(PARAMS["ra_dir"])
DEC_DIR = int(PARAMS["dec_dir"])

# 振り幅の微調整（小さいと感じる場合は 1.00 より少し上げる）
RA_GAIN = float(PARAMS["ra_gain"])
DEC_GAIN = float(PARAMS["dec_gain"])

# 軸ごとの変換係数
RA_SCALE = float(PARAMS["go2_ra_scale"])
DEC_SCALE = float(PARAMS["go2_dec_scale"])

# サーボ定義と同じ安全範囲にパルス幅を収める
MIN_PULSE_SEC = float(PARAMS["min_pulse_sec"])
MAX_PULSE_SEC = float(PARAMS["max_pulse_sec"])


def load_last_position():
    try:
        with STATE_FILE.open("r", encoding="ascii") as f:
            data = json.load(f)
        return {"ra": int(data["ra"]), "dec": int(data["dec"])}
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def save_last_position(ra, dec):
    try:
        with STATE_FILE.open("w", encoding="ascii") as f:
            json.dump({"ra": ra, "dec": dec}, f)
    except OSError as e:
        # 状態保存に失敗しても駆動自体は継続する
        print(f"warning: 状態保存に失敗しました: {STATE_FILE} ({e})", file=sys.stderr)


def build_pin_factory():
    if PiGPIOFactory is None:
        return None

    try:
        return PiGPIOFactory()
    except OSError:
        # pigpiod未起動時はNoneで返し、gpiozero標準へフォールバック
        return None


def clamp(value, min_value, max_value):
    return max(min_value, min(max_value, value))


def usage(script):
    print(
        f"usage: {script} [--force] ra dec | home\n"
        f"       {script} --interactive\n"
        " ra:-90~90 dec:-90~90"
    )


def parse_interactive_line(line):
    text = line.strip()
    lower = text.lower()
    if lower == "q":
        return "quit", None, None
    if lower in ("home", "h"):
        return "ok", 0, 0

    parts = text.split()
    if len(parts) != 2:
        return "invalid", None, None

    try:
        ra = int(parts[0])
        dec = int(parts[1])
    except ValueError:
        return "invalid", None, None

    ra = 0 if (ra < -90 or ra > 90) else ra
    dec = 0 if (dec < -90 or dec > 90) else dec
    ra = int(round(ra * RA_DIR))
    dec = int(round(dec * DEC_DIR))
    return "ok", ra, dec


def drive_once(ra, dec, pin_factory):
    pwm_ra_sec = (1500 - RA_SCALE * (ra * RA_GAIN)) / 1000000.0
    pwm_dec_sec = (1500 - DEC_SCALE * (dec * DEC_GAIN)) / 1000000.0
    pwm_ra_sec = clamp(pwm_ra_sec, MIN_PULSE_SEC, MAX_PULSE_SEC)
    pwm_dec_sec = clamp(pwm_dec_sec, MIN_PULSE_SEC, MAX_PULSE_SEC)

    ra_value = ((pwm_ra_sec - 0.0015) / 0.001)
    dec_value = ((pwm_dec_sec - 0.0015) / 0.001)

    servo_ra = Servo(
        RA_PIN,
        min_pulse_width=MIN_PULSE_SEC,
        max_pulse_width=MAX_PULSE_SEC,
        initial_value=ra_value,
        pin_factory=pin_factory,
    )
    servo_dec = Servo(
        DEC_PIN,
        min_pulse_width=MIN_PULSE_SEC,
        max_pulse_width=MAX_PULSE_SEC,
        initial_value=dec_value,
        pin_factory=pin_factory,
    )

    try:
        servo_ra.pulse_width = pwm_ra_sec
        servo_dec.pulse_width = pwm_dec_sec
        time.sleep(1)
    finally:
        servo_ra.close()
        servo_dec.close()


def run_interactive(pin_factory):
    current_ra = None
    current_dec = None
    print("入力待ちです。'ra dec'（例: 0 0 / 10 10）または home(h) を入力。終了は q")

    try:
        while True:
            line = input("> ")
            status, ra, dec = parse_interactive_line(line)

            if status == "quit":
                print("終了します。")
                return

            if status == "invalid":
                print("入力形式エラー: 'ra dec' または home(h) を入力してください。終了は q")
                continue

            if current_ra == ra and current_dec == dec:
                print("前回と同じ指令値のため、駆動をスキップしました。")
                continue

            drive_once(ra, dec, pin_factory)
            current_ra = ra
            current_dec = dec
            print(f"ok: ra={ra} dec={dec}")
    except KeyboardInterrupt:
        print("\n終了します。")

def main():
    args = sys.argv
    force = False
    interactive = False

    if "--force" in args:
        force = True
        args = [a for a in args if a != "--force"]

    if "--interactive" in args:
        interactive = True
        args = [a for a in args if a != "--interactive"]

    ac = len(args)
    ra = 0
    dec = 0
    err = False

    # コマンドライン引数の解析
    if interactive:
        if ac != 1:
            err = True
    elif ac == 2:
        if args[1] == "home": # ※前回のコードにあった引数アクセスのバグも合わせて修正しました
            ra = 0
            dec = 0
        else:
            err = True
    elif ac == 3:
        try:
            ra = int(args[1])
            dec = int(args[2])
            # 範囲チェックと丸め処理
            ra = 0 if (ra < -90 or ra > 90) else ra
            dec = 0 if (dec < -90 or dec > 90) else dec
            ra = int(round(ra * RA_DIR))
            dec = int(round(dec * DEC_DIR))
        except ValueError:
            err = True
    else:
        err = True

    if err:
        usage(sys.argv[0])
        sys.exit(1)

    pin_factory = build_pin_factory()
    if pin_factory is None:
        print("warning: pigpioバックエンド未使用です。software PWMのため振動しやすくなります。")

    if interactive:
        run_interactive(pin_factory)
        return

    last = load_last_position()
    move_ra = True
    move_dec = True
    if (not force) and (last is not None):
        move_ra = (last["ra"] != ra)
        move_dec = (last["dec"] != dec)

    if not move_ra and not move_dec:
        print("前回と同じ指令値のため、駆動をスキップしました。")
        return

    # パルス幅の計算（μs を秒に変換。1500μs = 0.0015s）
    pwm_ra_sec = (1500 - RA_SCALE * (ra * RA_GAIN)) / 1000000.0
    pwm_dec_sec = (1500 - DEC_SCALE * (dec * DEC_GAIN)) / 1000000.0
    pwm_ra_sec = clamp(pwm_ra_sec, MIN_PULSE_SEC, MAX_PULSE_SEC)
    pwm_dec_sec = clamp(pwm_dec_sec, MIN_PULSE_SEC, MAX_PULSE_SEC)

    # Servo.value(-1.0~1.0)へ変換
    ra_value = ((pwm_ra_sec - 0.0015) / 0.001)
    dec_value = ((pwm_dec_sec - 0.0015) / 0.001)

    servo_ra = None
    servo_dec = None

    # 変化した軸だけを初期化して不要な微動を抑える
    if move_ra:
        servo_ra = Servo(
            RA_PIN,
            min_pulse_width=MIN_PULSE_SEC,
            max_pulse_width=MAX_PULSE_SEC,
            initial_value=ra_value,
            pin_factory=pin_factory,
        )
    if move_dec:
        servo_dec = Servo(
            DEC_PIN,
            min_pulse_width=MIN_PULSE_SEC,
            max_pulse_width=MAX_PULSE_SEC,
            initial_value=dec_value,
            pin_factory=pin_factory,
        )

    try:
        # パルス幅を直接指定してサーボを駆動
        if servo_ra is not None:
            servo_ra.pulse_width = pwm_ra_sec
        if servo_dec is not None:
            servo_dec.pulse_width = pwm_dec_sec
        
        # 1秒間パルスを維持（移動時間を確保）
        time.sleep(1)
        save_last_position(ra, dec)

    finally:
        # パルスを停止しピンを安全に解放
        if servo_ra is not None:
            servo_ra.close()
        if servo_dec is not None:
            servo_dec.close()

if __name__ == "__main__":
    main()

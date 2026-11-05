// リリース版では、Windowsで黒いコンソール画面を出さない
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    ibis_desktop_lib::run()
}

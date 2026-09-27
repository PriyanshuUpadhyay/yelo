mod hud;
mod profile;
mod setup;
mod usage;

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.as_slice() == ["--version"] {
        println!("yelo {}", env!("CARGO_PKG_VERSION"));
        return;
    }
    if args.as_slice() == ["--help"] {
        println!(
            "usage: yelo {{profile,usage,setup,doctor,hud}} ...\n\nManage profiles and usage.\n"
        );
        return;
    }
    if args.get(1).is_some_and(|s| s == "--help") {
        match args[0].as_str() {
            "profile" => println!(
                "usage: yelo profile {{list,resolve,menu,pick,sessions,owner,create,sync,model,codex-model}} ..."
            ),
            "usage" => println!("usage: yelo usage {{show,fetch,doctor}} ..."),
            "setup" => println!("usage: yelo setup [launchers|profiles|profile-mirrors] [--json]"),
            "doctor" => println!("usage: yelo doctor [--json]"),
            "hud" => println!("usage: yelo hud {{install,assemble,start,stop}} ..."),
            _ => {
                eprintln!("yelo: invalid command: {}", args[0]);
                std::process::exit(2);
            }
        }
        return;
    }
    if args.first().map(String::as_str) == Some("doctor")
        && let Some(arg) = args.iter().skip(1).find(|s| s.as_str() != "--json")
    {
        eprintln!(
            "usage: yelo doctor [-h] [--json]\nyelo doctor: error: unrecognized arguments: {arg}"
        );
        std::process::exit(2);
    }
    std::process::exit(match args.first().map(String::as_str) {
        Some("usage") => usage::run(&args),
        Some("setup") => setup::run(&args),
        Some("doctor") => setup::doctor(&args),
        Some("hud") => hud::run(&args),
        _ => profile::run(&args),
    });
}

mod profile;
mod usage;

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.as_slice() == ["--version"] {
        println!("yelo {}", env!("CARGO_PKG_VERSION"));
        return;
    }
    std::process::exit(if args.first().map(String::as_str) == Some("usage") {
        usage::run(&args)
    } else {
        profile::run(&args)
    });
}

//! What kind of machine this is, beyond how much memory it has.
//!
//! The installer used to know three things: memory, disks, and whether there
//! was a network. That is enough to say whether a kit fits and nothing else.
//! A kit that wants a graphics card, or one that only makes sense on a
//! machine somebody sits in front of, needs more.
//!
//! Everything here reads sysfs and /proc. Nothing is added to the image for
//! it, nothing is run, and a machine that does not answer simply reports
//! less. ⚠️ Video memory is deliberately absent: it cannot be read here. The
//! file that reports it belongs to the AMD driver, NVIDIA answers only
//! through its own, and the PCI apertures are a different number. The card is
//! identified; how much memory it has is looked up elsewhere.

use std::fs;
use std::path::Path;

/// What the box is, from its firmware. Not a guess: SMBIOS carries it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Chassis {
    Desktop,
    Laptop,
    AllInOne,
    Server,
    Other,
    Unknown,
}

impl Chassis {
    /// SMBIOS chassis types, from the specification's table.
    pub fn from_code(code: u32) -> Chassis {
        match code {
            3 | 4 | 5 | 6 | 7 | 15 | 16 => Chassis::Desktop,
            8 | 9 | 10 | 11 | 12 | 14 | 31 | 32 => Chassis::Laptop,
            13 => Chassis::AllInOne,
            17 | 23 | 28 => Chassis::Server,
            0 | 1 | 2 => Chassis::Unknown,
            _ => Chassis::Other,
        }
    }
    pub fn label(self) -> &'static str {
        match self {
            Chassis::Desktop => "desktop",
            Chassis::Laptop => "laptop",
            Chassis::AllInOne => "all-in-one",
            Chassis::Server => "server",
            Chassis::Other => "other",
            Chassis::Unknown => "unknown",
        }
    }
}

/// One display adapter on the PCI bus.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Gpu {
    /// `10de:2684` — what the card list is keyed on.
    pub id: String,
    pub vendor: String,
    /// The firmware chose this one to boot on.
    pub primary: bool,
    /// ⚠️ A server's management chip is a display adapter and is not a
    /// graphics card. Offering to run models on an ASPEED would be absurd,
    /// and these parts are in nearly every rack machine.
    pub usable: bool,
}

/// The whole picture, as far as it can be read.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Machine {
    pub chassis: Option<&'static str>,
    pub cpu: Option<String>,
    pub cores: usize,
    pub gpus: Vec<Gpu>,
}

impl Machine {
    pub fn read() -> Machine {
        Machine::read_under(Path::new("/sys"), Path::new("/proc/cpuinfo"))
    }

    /// Split out so it can be tested against a fabricated tree.
    pub fn read_under(sys: &Path, cpuinfo: &Path) -> Machine {
        let chassis = fs::read_to_string(sys.join("class/dmi/id/chassis_type"))
            .ok()
            .and_then(|s| s.trim().parse::<u32>().ok())
            .map(|c| Chassis::from_code(c).label());

        let info = fs::read_to_string(cpuinfo).unwrap_or_default();
        let cpu = info
            .lines()
            .find(|l| l.starts_with("model name"))
            .and_then(|l| l.split_once(':'))
            .map(|(_, v)| v.trim().to_string())
            .filter(|s| !s.is_empty());
        let cores = info.lines().filter(|l| l.starts_with("processor")).count();

        Machine { chassis, cpu, cores, gpus: gpus_under(&sys.join("bus/pci/devices")) }
    }

    /// The card a kit would actually use, if there is one.
    pub fn best_gpu(&self) -> Option<&Gpu> {
        self.gpus.iter().filter(|g| g.usable).max_by_key(|g| g.primary)
    }
}

/// Vendors whose display adapters are graphics cards, and vendors whose
/// display adapters are a server's remote console.
fn vendor_name(id: &str) -> (&'static str, bool) {
    match id {
        "10de" => ("NVIDIA", true),
        "1002" | "1022" => ("AMD", true),
        "8086" => ("Intel", true),
        "1a03" => ("ASPEED", false),
        "102b" => ("Matrox", false),
        "1234" | "1b36" | "15ad" | "80ee" => ("virtual", false),
        _ => ("unknown", false),
    }
}

fn gpus_under(devices: &Path) -> Vec<Gpu> {
    let Ok(entries) = fs::read_dir(devices) else { return Vec::new() };
    let mut paths: Vec<_> = entries.filter_map(|e| e.ok().map(|e| e.path())).collect();
    paths.sort();
    let mut out = Vec::new();
    for dev in paths {
        let class = fs::read_to_string(dev.join("class")).unwrap_or_default();
        // 0x03xxxx is "display controller" in the PCI class table.
        if !class.trim().starts_with("0x03") {
            continue;
        }
        let hex = |f: &str| {
            fs::read_to_string(dev.join(f))
                .ok()
                .map(|s| s.trim().trim_start_matches("0x").to_lowercase())
                .filter(|s| !s.is_empty())
        };
        let (Some(vendor), Some(device)) = (hex("vendor"), hex("device")) else { continue };
        let (name, usable) = vendor_name(&vendor);
        out.push(Gpu {
            id: format!("{vendor}:{device}"),
            vendor: name.to_string(),
            primary: fs::read_to_string(dev.join("boot_vga")).map(|s| s.trim() == "1").unwrap_or(false),
            usable,
        });
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn write(p: &Path, name: &str, text: &str) {
        fs::create_dir_all(p).unwrap();
        fs::write(p.join(name), text).unwrap();
    }

    #[test]
    fn chassis_codes_become_the_shapes_we_care_about() {
        assert_eq!(Chassis::from_code(3), Chassis::Desktop);
        assert_eq!(Chassis::from_code(10), Chassis::Laptop);
        assert_eq!(Chassis::from_code(13), Chassis::AllInOne);
        assert_eq!(Chassis::from_code(23), Chassis::Server);
        assert_eq!(Chassis::from_code(2), Chassis::Unknown);
    }

    /// ⚠️ Nearly every rack machine has a display adapter that is a remote
    /// console chip. Counting one as a graphics card would offer to run
    /// models on it.
    #[test]
    fn a_management_chip_is_not_a_graphics_card() {
        let tmp = std::env::temp_dir().join(format!("gpu-bmc-{}", std::process::id()));
        let devs = tmp.join("devices");
        for (slot, vendor, device, class, boot) in [
            ("0000:00:02.0", "0x1a03", "0x2000", "0x030000", "0"),
            ("0000:01:00.0", "0x10de", "0x2684", "0x030000", "1"),
            ("0000:02:00.0", "0x8086", "0x1234", "0x010601", "0"),
        ] {
            let d = devs.join(slot);
            write(&d, "vendor", vendor);
            write(&d, "device", device);
            write(&d, "class", class);
            write(&d, "boot_vga", boot);
        }
        let gpus = gpus_under(&devs);
        assert_eq!(gpus.len(), 2, "the storage controller is not a display adapter: {gpus:?}");
        let aspeed = gpus.iter().find(|g| g.vendor == "ASPEED").unwrap();
        assert!(!aspeed.usable, "a management chip must not count");
        let nv = gpus.iter().find(|g| g.vendor == "NVIDIA").unwrap();
        assert!(nv.usable && nv.primary);
        assert_eq!(nv.id, "10de:2684", "the id is what the card list is keyed on");

        let m = Machine { chassis: None, cpu: None, cores: 0, gpus };
        assert_eq!(m.best_gpu().map(|g| g.id.as_str()), Some("10de:2684"));
        let _ = fs::remove_dir_all(&tmp);
    }

    #[test]
    fn a_machine_with_only_a_management_chip_has_no_usable_card() {
        let tmp = std::env::temp_dir().join(format!("gpu-none-{}", std::process::id()));
        let d = tmp.join("devices/0000:00:02.0");
        write(&d, "vendor", "0x1a03");
        write(&d, "device", "0x2000");
        write(&d, "class", "0x030000");
        let m = Machine { chassis: None, cpu: None, cores: 0, gpus: gpus_under(&tmp.join("devices")) };
        assert!(m.best_gpu().is_none());
        let _ = fs::remove_dir_all(&tmp);
    }

    #[test]
    fn the_cpu_and_chassis_come_off_the_machine() {
        let tmp = std::env::temp_dir().join(format!("machine-{}", std::process::id()));
        write(&tmp.join("class/dmi/id"), "chassis_type", "13\n");
        fs::create_dir_all(tmp.join("bus/pci/devices")).unwrap();
        let cpuinfo = tmp.join("cpuinfo");
        fs::write(&cpuinfo, "processor\t: 0\nmodel name\t: Some CPU @ 3.00GHz\nprocessor\t: 1\nmodel name\t: Some CPU @ 3.00GHz\n").unwrap();
        let m = Machine::read_under(&tmp, &cpuinfo);
        assert_eq!(m.chassis, Some("all-in-one"));
        assert_eq!(m.cpu.as_deref(), Some("Some CPU @ 3.00GHz"));
        assert_eq!(m.cores, 2);
        let _ = fs::remove_dir_all(&tmp);
    }
}

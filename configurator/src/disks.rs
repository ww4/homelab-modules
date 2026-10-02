//! The disks this machine can see, by stable id — what the TUI's picker shows
//! and what the answers file wants (`/dev/disk/by-id/…` never shuffles the
//! way `/dev/sdX` does across reboots and USB re-plugs).

use std::fs;
use std::path::{Path, PathBuf};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Disk {
    /// `/dev/disk/by-id/ata-…` — the path the answers file carries.
    pub id: PathBuf,
    /// The kernel name it resolves to right now (`sda`), for display.
    pub kernel: String,
    pub size_bytes: u64,
    pub model: String,
    pub transport: String,
    /// The disk the running system booted from (mounted / holding `/nix`) — the
    /// picker refuses to offer it, so a live USB cannot erase itself.
    pub in_use: bool,
}

impl Disk {
    pub fn size_human(&self) -> String {
        let gb = self.size_bytes as f64 / 1e9;
        if gb >= 1000.0 { format!("{:.1} TB", gb / 1000.0) } else { format!("{:.0} GB", gb) }
    }
}

/// Whole disks under /dev/disk/by-id, one entry per kernel device, preferring
/// the most specific id (`nvme-<model>_<serial>`, `ata-…`, `usb-…`) over
/// `wwn-…`/`nvme-eui…` aliases. Partitions and device-mapper nodes are skipped.
pub fn list() -> Vec<Disk> {
    list_under(Path::new("/dev/disk/by-id"), Path::new("/sys/class/block"), &mounted_kernels())
}

fn list_under(by_id: &Path, sys_block: &Path, mounted: &[String]) -> Vec<Disk> {
    let mut by_kernel: std::collections::BTreeMap<String, Disk> = Default::default();
    let Ok(entries) = fs::read_dir(by_id) else { return Vec::new() };
    let mut names: Vec<PathBuf> = entries.filter_map(|e| e.ok().map(|e| e.path())).collect();
    names.sort();
    for p in names {
        let fname = p.file_name().and_then(|n| n.to_str()).unwrap_or("").to_string();
        if fname.contains("-part") || fname.starts_with("dm-") || fname.starts_with("lvm-") || fname.starts_with("md-") {
            continue;
        }
        let Ok(target) = fs::read_link(&p) else { continue };
        let kernel = target.file_name().and_then(|n| n.to_str()).unwrap_or("").to_string();
        if kernel.is_empty() || kernel.starts_with("dm-") || kernel.starts_with("md") || kernel.starts_with("sr") || kernel.starts_with("loop") {
            continue;
        }
        let specific = !(fname.starts_with("wwn-") || fname.starts_with("nvme-eui") || fname.starts_with("scsi-"));
        let entry = by_kernel.entry(kernel.clone());
        match entry {
            std::collections::btree_map::Entry::Occupied(mut o) => {
                let cur = o.get().id.file_name().and_then(|n| n.to_str()).unwrap_or("").to_string();
                let cur_specific = !(cur.starts_with("wwn-") || cur.starts_with("nvme-eui") || cur.starts_with("scsi-"));
                if specific && !cur_specific {
                    o.get_mut().id = p.clone();
                }
            }
            std::collections::btree_map::Entry::Vacant(v) => {
                let sysdir = sys_block.join(&kernel);
                let size_bytes = fs::read_to_string(sysdir.join("size")).ok().and_then(|s| s.trim().parse::<u64>().ok()).unwrap_or(0) * 512;
                let model = fs::read_to_string(sysdir.join("device/model")).map(|s| s.trim().to_string()).unwrap_or_default();
                let transport = if kernel.starts_with("nvme") {
                    "nvme".into()
                } else if fname.starts_with("usb-") {
                    "usb".into()
                } else if fname.starts_with("ata-") {
                    "sata".into()
                } else {
                    String::new()
                };
                let in_use = mounted.iter().any(|m| m == &kernel);
                v.insert(Disk { id: p.clone(), kernel, size_bytes, model, transport, in_use });
            }
        }
    }
    let mut out: Vec<Disk> = by_kernel.into_values().collect();
    out.sort_by(|a, b| (a.in_use, &a.transport, std::cmp::Reverse(a.size_bytes)).cmp(&(b.in_use, &b.transport, std::cmp::Reverse(b.size_bytes))));
    out
}

/// Kernel names of disks holding a currently mounted filesystem (from
/// /proc/self/mounts), partitions folded back to their disk.
fn mounted_kernels() -> Vec<String> {
    let Ok(text) = fs::read_to_string("/proc/self/mounts") else { return Vec::new() };
    let mut out = Vec::new();
    for line in text.lines() {
        let Some(dev) = line.split_whitespace().next() else { continue };
        let Some(name) = dev.strip_prefix("/dev/") else { continue };
        let disk = if name.starts_with("nvme") {
            name.split('p').next().unwrap_or(name).to_string()
        } else {
            name.trim_end_matches(|c: char| c.is_ascii_digit()).to_string()
        };
        if !out.contains(&disk) {
            out.push(disk);
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::symlink;

    #[test]
    fn by_id_listing_prefers_specific_ids_and_skips_partitions() {
        let tmp = std::env::temp_dir().join(format!("disks-test-{}", std::process::id()));
        let by_id = tmp.join("by-id");
        let sys = tmp.join("sys");
        fs::create_dir_all(&by_id).unwrap();
        for k in ["sda", "nvme0n1"] {
            fs::create_dir_all(sys.join(k).join("device")).unwrap();
            fs::write(sys.join(k).join("size"), "7814037168\n").unwrap();
            fs::write(sys.join(k).join("device/model"), format!("Model {k}\n")).unwrap();
        }
        symlink("../../sda", by_id.join("ata-WDC_WD40_SERIAL")).unwrap();
        symlink("../../sda", by_id.join("wwn-0x5000c500")).unwrap();
        symlink("../../sda1", by_id.join("ata-WDC_WD40_SERIAL-part1")).unwrap();
        symlink("../../nvme0n1", by_id.join("nvme-eui.0025")).unwrap();
        symlink("../../nvme0n1", by_id.join("nvme-Samsung_970_SERIAL")).unwrap();
        let disks = list_under(&by_id, &sys, &["nvme0n1".to_string()]);
        fs::remove_dir_all(&tmp).ok();
        assert_eq!(disks.len(), 2);
        let sda = disks.iter().find(|d| d.kernel == "sda").unwrap();
        assert!(sda.id.ends_with("ata-WDC_WD40_SERIAL"), "{:?}", sda.id);
        assert_eq!(sda.size_human(), "4.0 TB");
        assert!(!sda.in_use);
        let nvme = disks.iter().find(|d| d.kernel == "nvme0n1").unwrap();
        assert!(nvme.id.ends_with("nvme-Samsung_970_SERIAL"), "{:?}", nvme.id);
        assert!(nvme.in_use, "the booted disk is marked in use");
        assert_eq!(disks[0].kernel, "sda", "in-use disks sort last");
    }
}

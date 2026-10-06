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

/// Kernel names of the disks this running system is sitting on.
///
/// ⚠️ A MOUNT LIST IS NOT ENOUGH, AND THE GAP ERASES PEOPLE'S BOOT MEDIA.
/// This used to read /proc/self/mounts and fold partitions back to their
/// disk. On a stick written with a tool like Ventoy the ISO is a FILE on the
/// stick, presented to the kernel as a device-mapper or loop device, and the
/// stick's own partitions are never mounted. So the stick holding the running
/// installer looked completely free, was offered as a data disk, and disko
/// repartitioned it. It got as far as writing a new partition table before
/// mkfs refused because the kernel still had the device open.
///
/// So: start from every mounted device and walk DOWN to the hardware. A
/// device-mapper or md device names its components in `slaves/`; a loop
/// device backed by a file names the file in `loop/backing_file`, and the
/// filesystem holding that file is itself mounted from something. Follow all
/// of it, and mark every real disk reached.
fn mounted_kernels() -> Vec<String> {
    let mounts = fs::read_to_string("/proc/self/mounts").unwrap_or_default();
    in_use_disks(&mounts, Path::new("/sys/block"))
}

/// The disk a kernel device name belongs to: a partition folded back to its
/// whole disk, anything else left alone.
fn whole_disk(name: &str) -> String {
    if name.starts_with("nvme") || name.starts_with("mmcblk") || name.starts_with("loop") {
        // nvme0n1p2 -> nvme0n1, mmcblk0p1 -> mmcblk0, loop3p1 -> loop3
        match name.rfind('p') {
            Some(i) if name[i + 1..].chars().all(|c| c.is_ascii_digit()) && !name[i + 1..].is_empty() => name[..i].to_string(),
            _ => name.to_string(),
        }
    } else if name.starts_with("dm-") || name.starts_with("md") {
        name.to_string()
    } else {
        name.trim_end_matches(|c: char| c.is_ascii_digit()).to_string()
    }
}

/// Every real disk underneath the given mounts. Pure, so it can be tested
/// against a fabricated /sys/block.
pub fn in_use_disks(mounts: &str, sys_block: &Path) -> Vec<String> {
    let mut todo: Vec<String> = Vec::new();
    // Where a mounted filesystem lives, so a loop's backing file can be
    // traced to the device under it: (mount point, device).
    let mut points: Vec<(String, String)> = Vec::new();
    for line in mounts.lines() {
        let mut f = line.split_whitespace();
        let (Some(dev), Some(point)) = (f.next(), f.next()) else { continue };
        let Some(name) = dev.strip_prefix("/dev/") else { continue };
        points.push((point.to_string(), name.to_string()));
        todo.push(name.to_string());
    }

    let mut seen: std::collections::BTreeSet<String> = Default::default();
    let mut out: Vec<String> = Vec::new();
    while let Some(name) = todo.pop() {
        let disk = whole_disk(&name);
        if !seen.insert(disk.clone()) {
            continue;
        }
        let dir = sys_block.join(&disk);
        // A stack of other devices: follow every one of them.
        if let Ok(slaves) = fs::read_dir(dir.join("slaves")) {
            for e in slaves.flatten() {
                if let Some(n) = e.file_name().to_str() {
                    todo.push(n.to_string());
                }
            }
        }
        // A loop over a file: whatever holds that file is also in use.
        if let Ok(backing) = fs::read_to_string(dir.join("loop/backing_file")) {
            let file = backing.trim();
            // The longest mount point the file sits under is its filesystem.
            if let Some((_, dev)) = points
                .iter()
                .filter(|(point, _)| file == point || file.starts_with(&format!("{}/", point.trim_end_matches('/'))))
                .max_by_key(|(point, _)| point.len())
            {
                todo.push(dev.clone());
            }
        }
        if !disk.starts_with("dm-") && !disk.starts_with("md") && !disk.starts_with("loop") {
            out.push(disk);
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::symlink;

    /// ⚠️ THE BUG THIS EXISTS FOR. A stick written with Ventoy holds the ISO
    /// as a file and presents it through device-mapper, so none of the
    /// stick's own partitions are mounted and a plain mount list shows it as
    /// free. It was offered as a data disk and repartitioned while the
    /// installer was running from it.
    #[test]
    fn a_boot_stick_behind_device_mapper_counts_as_in_use() {
        let tmp = std::env::temp_dir().join(format!("inuse-dm-{}", std::process::id()));
        let sys = tmp.join("sys");
        fs::create_dir_all(sys.join("dm-0/slaves/sda1")).unwrap();
        fs::create_dir_all(sys.join("sda")).unwrap();
        fs::create_dir_all(sys.join("sdb")).unwrap();

        // Nothing on sda is mounted; only the mapped device is.
        let mounts = "/dev/dm-0 /iso iso9660 ro 0 0\ntmpfs /run tmpfs rw 0 0\n";
        let got = in_use_disks(mounts, &sys);
        assert!(got.contains(&"sda".to_string()), "the stick must be in use, got {got:?}");
        assert!(!got.contains(&"sdb".to_string()), "an untouched disk is still offered, got {got:?}");
        let _ = fs::remove_dir_all(&tmp);
    }

    /// The other shape: a loop device over a FILE that lives on a mounted
    /// filesystem. The disk under that filesystem is in use too.
    #[test]
    fn a_loop_over_a_file_counts_the_disk_holding_the_file() {
        let tmp = std::env::temp_dir().join(format!("inuse-loop-{}", std::process::id()));
        let sys = tmp.join("sys");
        fs::create_dir_all(sys.join("loop0/loop")).unwrap();
        fs::create_dir_all(sys.join("sdb")).unwrap();
        fs::create_dir_all(sys.join("sdc")).unwrap();
        fs::write(sys.join("loop0/loop/backing_file"), "/iso/nixos.iso\n").unwrap();

        let mounts = "/dev/loop0 /nix/.ro-store squashfs ro 0 0\n/dev/sdb1 /iso vfat rw 0 0\n";
        let got = in_use_disks(mounts, &sys);
        assert!(got.contains(&"sdb".to_string()), "the disk holding the ISO file is in use, got {got:?}");
        assert!(!got.contains(&"sdc".to_string()), "and nothing else is, got {got:?}");
        let _ = fs::remove_dir_all(&tmp);
    }

    #[test]
    fn a_plainly_mounted_partition_still_counts() {
        let tmp = std::env::temp_dir().join(format!("inuse-plain-{}", std::process::id()));
        let sys = tmp.join("sys");
        fs::create_dir_all(sys.join("nvme0n1")).unwrap();
        let got = in_use_disks("/dev/nvme0n1p2 / ext4 rw 0 0\n", &sys);
        assert_eq!(got, vec!["nvme0n1".to_string()]);
        let _ = fs::remove_dir_all(&tmp);
    }

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

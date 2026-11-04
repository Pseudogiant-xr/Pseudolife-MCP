use super::*;

const MIN_FREE: u64 = 8 * 1024 * 1024 * 1024;

fn require(free: u64, location: &str) -> Result<()> {
    if free < MIN_FREE {
        return Err(UpdateError::usage(format!(
            "less than 8 GiB is free on {location}; free at least 8 GiB there, then retry. Nothing was changed"
        )));
    }
    Ok(())
}

pub(super) fn host(path: &Path) -> Result<()> {
    let mut target = path;
    while !target.exists() {
        target = target.parent().ok_or_else(|| UpdateError::usage("cannot find the destination filesystem; restore its directory and retry. Nothing was changed"))?;
    }
    let available = crate::runtime::available_space(target).map_err(|_| UpdateError::usage(format!(
        "cannot check free space for {}; restore access to that filesystem and retry. Nothing was changed", path.display()
    )))?;
    require(available, &path.display().to_string())
}

fn available(output: &str) -> Result<u64> {
    output.lines().last().and_then(|line| line.split_whitespace().nth(3))
        .and_then(|value| value.parse::<u64>().ok()).and_then(|value| value.checked_mul(1024))
        .ok_or_else(|| UpdateError::usage("cannot read Docker free space; make `df -Pk` work in the container and retry. Nothing was changed"))
}

impl Update {
    pub fn disk_container(&self, name: &str, path: &str) -> Result<()> {
        let (code, output) = self.docker_call(&["exec", name, "df", "-Pk", path]);
        if code != 0 {
            return Err(UpdateError::usage(format!(
                "cannot check free space on {name}:{path}; run `docker exec {name} df -Pk {path}` successfully, then retry. Nothing was changed"
            )));
        }
        require(available(&output)?, &format!("{name}:{path}"))
    }

    pub fn disk_storage(&mut self, daemon: &str, pg: &str, image: Option<&str>) -> Result<()> {
        for name in [pg, daemon] {
            let (code, output) = self.docker_call(&["exec", name, "df", "-Pk", "/"]);
            if code == 0 {
                return require(available(&output)?, "Docker storage");
            }
        }
        let Some(image) = image else {
            self.warn("Docker storage space could not be checked before this first build; the host filesystem check passed. Keep at least 8 GiB free in Docker's data storage");
            return Ok(());
        };
        let helper = format!("pseudolife-diskcheck-{}", uuid::Uuid::new_v4().simple());
        let created =
            self.docker_call(&[
                "create",
                "--name",
                &helper,
                "--read-only",
                "--network",
                "none",
                "--entrypoint",
                "df",
                image,
                "-Pk",
                "/",
            ])
            .0 == 0;
        let (code, output) = if created {
            self.docker_call(&["start", "-a", &helper])
        } else {
            (1, String::new())
        };
        let checked = if !created {
            Err(UpdateError::usage(
                "cannot check Docker storage space; restore Docker's ability to create a container from the current daemon image and retry. Nothing was changed",
            ))
        } else if code == 0 {
            available(&output).and_then(|free| require(free, "Docker storage"))
        } else {
            Err(UpdateError::usage(
                "cannot check Docker storage space; make `df -Pk /` work in the current daemon image and retry. Nothing was changed",
            ))
        };
        let cleanup = self.docker_call(&["rm", "-f", "-v", &helper]).0;
        if created && cleanup != 0 {
            return Err(UpdateError::usage(format!(
                "the disk-check helper could not be removed; run `docker rm -f -v {helper}` and retry. The daemon was not changed"
            )));
        }
        checked
    }

    pub fn disk_backup(&mut self, directory: &Path, pg: &str, daemon: &str) -> Result<()> {
        host(directory)?;
        self.disk_container(pg, "/tmp")?;
        self.disk_storage(daemon, pg, None)?;
        // A stopped daemon cannot write its state archive; keep the existing
        // SQL-only recovery path and its explicit missing-state warning.
        let (code, state) = self.docker_call(&["inspect", "-f", "{{.State.Running}}", daemon]);
        if code != 0 || state.trim() != "false" {
            self.disk_container(daemon, "/tmp")?;
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn disk_floor_and_df_units_fail_closed_at_the_boundary() {
        assert!(require(MIN_FREE - 1, "fixture").is_err());
        require(MIN_FREE, "fixture").unwrap();
        assert_eq!(available("Filesystem 1024-blocks Used Available Capacity Mounted on\noverlay 99999999 1 8388608 1% /\n").unwrap(), MIN_FREE);
        for output in [
            "",
            "not a df response",
            "overlay 1 1 -1 1% /",
            "overlay 1 1 18446744073709551615 1% /",
        ] {
            assert!(available(output).is_err());
        }
    }
}

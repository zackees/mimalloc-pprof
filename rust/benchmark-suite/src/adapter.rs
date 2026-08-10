//! Safe boundary around the one allocator adapter linked into a child process.

#[cfg(benchmark_native_adapter)]
use std::ffi::CStr;
use std::fmt;
use std::ptr::NonNull;

pub const BUILD_ALLOCATOR_ID: &str = env!("BENCH_ALLOCATOR_ID");
pub const BUILD_ALLOCATOR_VERSION: &str = env!("BENCH_ALLOCATOR_VERSION");
pub const BUILD_SOURCE_SHA: &str = env!("BENCH_ALLOCATOR_SOURCE_SHA");
pub const BUILD_LIBRARY_SHA256: &str = env!("BENCH_ALLOCATOR_LIBRARY_SHA256");

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct LinkedAllocatorIdentity {
    pub allocator_id: &'static str,
    pub allocator_version: &'static str,
    pub source_sha: &'static str,
    pub library_sha256: &'static str,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct AdapterSmoke {
    pub identity: LinkedAllocatorIdentity,
    pub checksum: u64,
    pub usable_size: usize,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AdapterError {
    Unlinked,
    InvalidIdentity(&'static str),
    IdentityMismatch {
        field: &'static str,
        expected: &'static str,
        actual: String,
    },
    InvalidRequest(&'static str),
    AllocationFailed(&'static str),
    AlignedAllocationFailed(i32),
    ContractViolation(&'static str),
}

impl fmt::Display for AdapterError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Unlinked => write!(formatter, "benchmark child has no native allocator adapter"),
            Self::InvalidIdentity(field) => write!(formatter, "adapter returned invalid {field}"),
            Self::IdentityMismatch {
                field,
                expected,
                actual,
            } => write!(
                formatter,
                "linked adapter {field} mismatch: expected {expected:?}, got {actual:?}"
            ),
            Self::InvalidRequest(message) => {
                write!(formatter, "invalid adapter request: {message}")
            }
            Self::AllocationFailed(operation) => {
                write!(formatter, "allocator returned null from {operation}")
            }
            Self::AlignedAllocationFailed(code) => {
                write!(formatter, "aligned allocation failed with errno {code}")
            }
            Self::ContractViolation(message) => {
                write!(formatter, "allocator adapter contract violation: {message}")
            }
        }
    }
}

impl std::error::Error for AdapterError {}

/// Validate the identity returned by the linked C adapter against the build
/// envelope embedded in this benchmark child. Keeping this check independently
/// testable prevents a wrong adapter from passing merely because native-link
/// integration tests were skipped on a developer host.
pub fn validate_runtime_identity(
    actual_id: &str,
    actual_version: &str,
) -> Result<(), AdapterError> {
    if actual_id != BUILD_ALLOCATOR_ID {
        return Err(AdapterError::IdentityMismatch {
            field: "ID",
            expected: BUILD_ALLOCATOR_ID,
            actual: actual_id.to_owned(),
        });
    }
    if actual_version != BUILD_ALLOCATOR_VERSION {
        return Err(AdapterError::IdentityMismatch {
            field: "version",
            expected: BUILD_ALLOCATOR_VERSION,
            actual: actual_version.to_owned(),
        });
    }
    Ok(())
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct LinkedAdapter {
    identity: LinkedAllocatorIdentity,
}

impl LinkedAdapter {
    #[cfg(benchmark_native_adapter)]
    pub fn load() -> Result<Self, AdapterError> {
        let actual_id = unsafe { checked_identity(bench_allocator_id(), "allocator ID")? };
        let actual_version = unsafe { checked_identity(bench_allocator_version(), "version")? };
        validate_runtime_identity(actual_id, actual_version)?;
        Ok(Self {
            identity: LinkedAllocatorIdentity {
                allocator_id: BUILD_ALLOCATOR_ID,
                allocator_version: BUILD_ALLOCATOR_VERSION,
                source_sha: BUILD_SOURCE_SHA,
                library_sha256: BUILD_LIBRARY_SHA256,
            },
        })
    }

    #[cfg(not(benchmark_native_adapter))]
    pub fn load() -> Result<Self, AdapterError> {
        Err(AdapterError::Unlinked)
    }

    pub fn identity(&self) -> LinkedAllocatorIdentity {
        self.identity
    }

    #[cfg(benchmark_native_adapter)]
    pub fn alloc(&self, size: usize) -> Result<NonNull<u8>, AdapterError> {
        if size == 0 {
            return Err(AdapterError::InvalidRequest(
                "allocation size must be nonzero",
            ));
        }
        NonNull::new(unsafe { bench_alloc(size) }.cast())
            .ok_or(AdapterError::AllocationFailed("alloc"))
    }

    #[cfg(not(benchmark_native_adapter))]
    pub fn alloc(&self, _size: usize) -> Result<NonNull<u8>, AdapterError> {
        Err(AdapterError::Unlinked)
    }

    #[cfg(benchmark_native_adapter)]
    pub fn calloc(&self, count: usize, size: usize) -> Result<NonNull<u8>, AdapterError> {
        if count == 0 || size == 0 || count.checked_mul(size).is_none() {
            return Err(AdapterError::InvalidRequest(
                "calloc dimensions must be nonzero and cannot overflow",
            ));
        }
        NonNull::new(unsafe { bench_calloc(count, size) }.cast())
            .ok_or(AdapterError::AllocationFailed("calloc"))
    }

    #[cfg(not(benchmark_native_adapter))]
    pub fn calloc(&self, _count: usize, _size: usize) -> Result<NonNull<u8>, AdapterError> {
        Err(AdapterError::Unlinked)
    }

    /// The caller must pass a live pointer returned by this adapter. On error,
    /// the original allocation remains live and must still be freed.
    #[cfg(benchmark_native_adapter)]
    pub unsafe fn realloc(
        &self,
        pointer: NonNull<u8>,
        size: usize,
    ) -> Result<NonNull<u8>, AdapterError> {
        if size == 0 {
            return Err(AdapterError::InvalidRequest(
                "reallocation size must be nonzero",
            ));
        }
        NonNull::new(unsafe { bench_realloc(pointer.as_ptr().cast(), size) }.cast())
            .ok_or(AdapterError::AllocationFailed("realloc"))
    }

    #[cfg(not(benchmark_native_adapter))]
    pub unsafe fn realloc(
        &self,
        _pointer: NonNull<u8>,
        _size: usize,
    ) -> Result<NonNull<u8>, AdapterError> {
        Err(AdapterError::Unlinked)
    }

    #[cfg(benchmark_native_adapter)]
    pub fn aligned_alloc(
        &self,
        alignment: usize,
        size: usize,
    ) -> Result<NonNull<u8>, AdapterError> {
        if size == 0 || alignment < std::mem::size_of::<*const ()>() || !alignment.is_power_of_two()
        {
            return Err(AdapterError::InvalidRequest(
                "aligned allocation requires nonzero size and pointer-sized power-of-two alignment",
            ));
        }
        let mut pointer = std::ptr::null_mut();
        let result = unsafe { bench_aligned_alloc(&mut pointer, alignment, size) };
        if result != 0 {
            return Err(AdapterError::AlignedAllocationFailed(result));
        }
        let pointer: NonNull<u8> =
            NonNull::new(pointer.cast()).ok_or(AdapterError::ContractViolation(
                "aligned allocation returned success with a null pointer",
            ))?;
        if pointer.as_ptr() as usize % alignment != 0 {
            unsafe { bench_free(pointer.as_ptr().cast()) };
            return Err(AdapterError::ContractViolation(
                "aligned allocation returned a misaligned pointer",
            ));
        }
        Ok(pointer)
    }

    #[cfg(not(benchmark_native_adapter))]
    pub fn aligned_alloc(
        &self,
        _alignment: usize,
        _size: usize,
    ) -> Result<NonNull<u8>, AdapterError> {
        Err(AdapterError::Unlinked)
    }

    /// The pointer must be live and must have been returned by this adapter.
    #[cfg(benchmark_native_adapter)]
    pub unsafe fn free(&self, pointer: NonNull<u8>) {
        unsafe { bench_free(pointer.as_ptr().cast()) }
    }

    #[cfg(not(benchmark_native_adapter))]
    pub unsafe fn free(&self, _pointer: NonNull<u8>) {}

    /// The pointer must be live and must have been returned by this adapter.
    #[cfg(benchmark_native_adapter)]
    pub unsafe fn usable_size(&self, pointer: NonNull<u8>) -> usize {
        unsafe { bench_usable_size(pointer.as_ptr().cast()) }
    }

    #[cfg(not(benchmark_native_adapter))]
    pub unsafe fn usable_size(&self, _pointer: NonNull<u8>) -> usize {
        0
    }

    pub fn smoke_test(&self) -> Result<AdapterSmoke, AdapterError> {
        let mut checksum = 0_u64;
        let original = self.alloc(64)?;
        unsafe {
            for index in 0..64 {
                original
                    .as_ptr()
                    .add(index)
                    .write((index as u8).wrapping_mul(17));
            }
        }
        let grown = match unsafe { self.realloc(original, 128) } {
            Ok(pointer) => pointer,
            Err(error) => {
                unsafe { self.free(original) };
                return Err(error);
            }
        };
        unsafe {
            for index in 0..64 {
                let expected = (index as u8).wrapping_mul(17);
                let actual = grown.as_ptr().add(index).read();
                if actual != expected {
                    self.free(grown);
                    return Err(AdapterError::ContractViolation(
                        "realloc did not preserve existing content",
                    ));
                }
                checksum = checksum.wrapping_mul(131).wrapping_add(u64::from(actual));
            }
        }
        let usable_size = unsafe { self.usable_size(grown) };
        if usable_size < 128 {
            unsafe { self.free(grown) };
            return Err(AdapterError::ContractViolation(
                "usable size is smaller than the requested allocation",
            ));
        }
        unsafe { self.free(grown) };

        let zeroed = self.calloc(32, 4)?;
        unsafe {
            for index in 0..128 {
                let value = zeroed.as_ptr().add(index).read();
                if value != 0 {
                    self.free(zeroed);
                    return Err(AdapterError::ContractViolation(
                        "calloc returned nonzero bytes",
                    ));
                }
                zeroed.as_ptr().add(index).write(0xa5);
                checksum = checksum.wrapping_add(0xa5);
            }
            self.free(zeroed);
        }

        let aligned = self.aligned_alloc(256, 513)?;
        unsafe {
            aligned.as_ptr().write(0x5a);
            aligned.as_ptr().add(512).write(0xc3);
            checksum = checksum.wrapping_add(u64::from(aligned.as_ptr().read()));
            checksum = checksum.wrapping_add(u64::from(aligned.as_ptr().add(512).read()));
            self.free(aligned);
        }
        Ok(AdapterSmoke {
            identity: self.identity,
            checksum,
            usable_size,
        })
    }
}

#[cfg(benchmark_native_adapter)]
unsafe fn checked_identity(
    pointer: *const std::os::raw::c_char,
    field: &'static str,
) -> Result<&'static str, AdapterError> {
    if pointer.is_null() {
        return Err(AdapterError::InvalidIdentity(field));
    }
    unsafe { CStr::from_ptr(pointer) }
        .to_str()
        .map_err(|_| AdapterError::InvalidIdentity(field))
}

#[cfg(benchmark_native_adapter)]
unsafe extern "C" {
    fn bench_allocator_id() -> *const std::os::raw::c_char;
    fn bench_allocator_version() -> *const std::os::raw::c_char;
    fn bench_alloc(size: usize) -> *mut std::os::raw::c_void;
    fn bench_calloc(count: usize, size: usize) -> *mut std::os::raw::c_void;
    fn bench_realloc(pointer: *mut std::os::raw::c_void, size: usize) -> *mut std::os::raw::c_void;
    fn bench_aligned_alloc(
        output: *mut *mut std::os::raw::c_void,
        alignment: usize,
        size: usize,
    ) -> i32;
    fn bench_free(pointer: *mut std::os::raw::c_void);
    fn bench_usable_size(pointer: *mut std::os::raw::c_void) -> usize;
}

#[cfg(test)]
mod tests {
    use super::*;

    #[cfg(not(benchmark_native_adapter))]
    #[test]
    fn ordinary_workspace_build_is_explicitly_unlinked() {
        assert_eq!(LinkedAdapter::load(), Err(AdapterError::Unlinked));
        assert_eq!(BUILD_ALLOCATOR_ID, "unlinked-test-adapter");
    }
}

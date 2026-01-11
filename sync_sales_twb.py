#!/usr/bin/env python3
"""
Script to sync Sales.twbx from Live/ to Live/Customers/
This script applies only the changes (diff) from the source file to the destination file.
"""

import subprocess
import sys
import tempfile
import zipfile
import shutil
from pathlib import Path

def sync_sales_twb():
    """Apply changes from Live/Sales.twbx to Live/Customers/Sales.twbx using git diff"""
    
    # Get the repository root (assuming script is in root)
    repo_root = Path(__file__).parent.absolute()
    
    source_file = repo_root / "Live" / "Sales.twbx"
    dest_file = repo_root / "Live" / "Customers" / "Sales.twbx"
    
    # Check if source file exists
    if not source_file.exists():
        print(f"Error: Source file not found: {source_file}")
        return 1
    
    # Create destination directory if it doesn't exist
    dest_file.parent.mkdir(parents=True, exist_ok=True)
    
    # Check if we're in a git repository
    try:
        subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=repo_root,
            capture_output=True,
            check=True
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("Error: Not in a git repository or git not found")
        return 1
    
    # Get the diff of the source file (changes in the current commit/staging area)
    # For pre-commit hook: get diff from staged changes
    # For GitHub Actions: get diff from the commit
    
    try:
        # Try to get staged diff first (for pre-commit hook)
        result = subprocess.run(
            ["git", "diff", "--cached", str(source_file)],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False
        )
        
        # If no staged changes, try to get diff from last commit (for GitHub Actions)
        if not result.stdout.strip():
            result = subprocess.run(
                ["git", "diff", "HEAD~1", "HEAD", "--", str(source_file)],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=False
            )
        
        # If still no diff, try to get diff from base branch (for PRs)
        if not result.stdout.strip():
            # For GitHub Actions PRs, try to get the merge base
            merge_base_result = subprocess.run(
                ["git", "merge-base", "HEAD", "origin/main"],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=False
            )
            if merge_base_result.returncode == 0 and merge_base_result.stdout.strip():
                merge_base = merge_base_result.stdout.strip()
                result = subprocess.run(
                    ["git", "diff", merge_base, "HEAD", "--", str(source_file)],
                    cwd=repo_root,
                    capture_output=True,
                    text=True,
                    check=False
                )
            else:
                # Fallback: try with master branch
                merge_base_result = subprocess.run(
                    ["git", "merge-base", "HEAD", "origin/master"],
                    cwd=repo_root,
                    capture_output=True,
                    text=True,
                    check=False
                )
                if merge_base_result.returncode == 0 and merge_base_result.stdout.strip():
                    merge_base = merge_base_result.stdout.strip()
                    result = subprocess.run(
                        ["git", "diff", merge_base, "HEAD", "--", str(source_file)],
                        cwd=repo_root,
                        capture_output=True,
                        text=True,
                        check=False
                    )
        
        diff_output = result.stdout.strip()
        
        # Check if git detected this as a binary file
        if "Binary files differ" in diff_output or "binary" in diff_output.lower():
            # For .twbx files (zip archives), we need to extract, diff, and repackage
            return apply_twbx_changes(source_file, dest_file, repo_root)
        
        if not diff_output:
            print("✓ No changes detected in source file")
            return 0
        
        # If destination file doesn't exist, just copy the source file
        if not dest_file.exists():
            import shutil
            shutil.copy2(source_file, dest_file)
            print(f"✓ Created {dest_file} from {source_file}")
            return 0
        
        # Create a temporary patch file
        with tempfile.NamedTemporaryFile(mode='w', suffix='.patch', delete=False) as patch_file:
            patch_file.write(diff_output)
            patch_path = patch_file.name
        
        try:
            # Apply the patch to the destination file
            # We need to modify the patch to point to the destination file
            # Read the patch and replace the file path
            with open(patch_path, 'r', encoding='utf-8') as f:
                patch_content = f.read()
            
            # Replace source file path with destination file path in the patch
            source_str = str(source_file).replace('\\', '/')
            dest_str = str(dest_file).replace('\\', '/')
            modified_patch = patch_content.replace(source_str, dest_str)
            
            # Write modified patch
            with open(patch_path, 'w', encoding='utf-8') as f:
                f.write(modified_patch)
            
            # Apply the patch using git apply
            apply_result = subprocess.run(
                ["git", "apply", "--ignore-whitespace", "--directory", str(dest_file.parent), patch_path],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=False
            )
            
            if apply_result.returncode == 0:
                print(f"✓ Applied changes from {source_file} to {dest_file}")
                return 0
            else:
                # If patch application fails, try using the 'patch' command as fallback
                print(f"Warning: git apply failed, trying alternative method...")
                print(f"Error: {apply_result.stderr}")
                
                # Fallback: use Python's difflib to apply changes
                # This is more complex but more reliable
                return apply_changes_manually(source_file, dest_file, diff_output)
        
        finally:
            # Clean up temporary patch file
            try:
                Path(patch_path).unlink()
            except:
                pass
    
    except Exception as e:
        print(f"Error applying changes: {e}")
        return 1

def apply_changes_manually(source_file, dest_file, diff_output):
    """Fallback method: use git apply with 3-way merge strategy"""
    import subprocess
    import tempfile
    from pathlib import Path
    
    try:
        repo_root = Path(__file__).parent.absolute()
        
        # Create a temporary patch file
        with tempfile.NamedTemporaryFile(mode='w', suffix='.patch', delete=False) as patch_file:
            # Modify the patch to work with destination file
            source_str = str(source_file).replace('\\', '/')
            dest_str = str(dest_file).replace('\\', '/')
            modified_patch = diff_output.replace(source_str, dest_str)
            patch_file.write(modified_patch)
            patch_path = patch_file.name
        
        try:
            # Try git apply with 3-way merge (more forgiving)
            apply_result = subprocess.run(
                ["git", "apply", "--3way", "--ignore-whitespace", patch_path],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=False
            )
            
            if apply_result.returncode == 0:
                print(f"✓ Applied changes using 3-way merge to {dest_file}")
                return 0
            else:
                print(f"Warning: Could not apply patch automatically")
                print(f"Error: {apply_result.stderr}")
                print(f"⚠ You may need to merge changes manually")
                return 1
        
        finally:
            try:
                Path(patch_path).unlink()
            except:
                pass
    
    except Exception as e:
        print(f"Error in manual application: {e}")
        return 1

def apply_twbx_changes(source_file, dest_file, repo_root):
    """Apply changes to .twbx files by extracting, diffing, and repackaging"""
    try:
        # Create temporary directories for extraction
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source_extracted = temp_path / "source"
            dest_extracted = temp_path / "dest"
            source_extracted.mkdir()
            dest_extracted.mkdir()
            
            # Extract source .twbx
            source_twb = None
            with zipfile.ZipFile(source_file, 'r') as source_zip:
                source_zip.extractall(source_extracted)
                # Find the .twb file inside
                twb_files = list(source_extracted.glob("*.twb"))
                if twb_files:
                    source_twb = twb_files[0]
                else:
                    print("Error: No .twb file found in source .twbx")
                    return 1
            
            # Extract or create destination .twbx
            if dest_file.exists():
                with zipfile.ZipFile(dest_file, 'r') as dest_zip:
                    dest_zip.extractall(dest_extracted)
                dest_twb_files = list(dest_extracted.glob("*.twb"))
                if dest_twb_files:
                    dest_twb = dest_twb_files[0]
                else:
                    # If no .twb in dest, copy from source
                    dest_twb = dest_extracted / source_twb.name
                    shutil.copy2(source_twb, dest_twb)
            else:
                # If dest doesn't exist, copy entire structure
                shutil.copytree(source_extracted, dest_extracted, dirs_exist_ok=True)
                dest_twb = dest_extracted / source_twb.name
            
            # Get the previous version from git
            prev_source_twb = None
            try:
                # Try multiple methods to get previous version
                # Method 1: For staged changes, get from HEAD
                result = subprocess.run(
                    ["git", "show", f"HEAD:{source_file}"],
                    cwd=repo_root,
                    capture_output=True,
                    check=False
                )
                
                # Method 2: If that fails, try HEAD~1
                if result.returncode != 0:
                    result = subprocess.run(
                        ["git", "show", f"HEAD~1:{source_file}"],
                        cwd=repo_root,
                        capture_output=True,
                        check=False
                    )
                
                # Method 3: Try to get from merge base (for PRs)
                if result.returncode != 0:
                    merge_base_result = subprocess.run(
                        ["git", "merge-base", "HEAD", "origin/main"],
                        cwd=repo_root,
                        capture_output=True,
                        text=True,
                        check=False
                    )
                    if merge_base_result.returncode == 0 and merge_base_result.stdout.strip():
                        merge_base = merge_base_result.stdout.strip()
                        result = subprocess.run(
                            ["git", "show", f"{merge_base}:{source_file}"],
                            cwd=repo_root,
                            capture_output=True,
                            check=False
                        )
                    else:
                        # Fallback: try with master branch
                        merge_base_result = subprocess.run(
                            ["git", "merge-base", "HEAD", "origin/master"],
                            cwd=repo_root,
                            capture_output=True,
                            text=True,
                            check=False
                        )
                        if merge_base_result.returncode == 0 and merge_base_result.stdout.strip():
                            merge_base = merge_base_result.stdout.strip()
                            result = subprocess.run(
                                ["git", "show", f"{merge_base}:{source_file}"],
                                cwd=repo_root,
                                capture_output=True,
                                check=False
                            )
                
                if result.returncode == 0 and result.stdout:
                    # Extract previous version
                    with tempfile.NamedTemporaryFile(suffix='.twbx', delete=False) as prev_twbx:
                        prev_twbx.write(result.stdout)
                        prev_twbx_path = prev_twbx.name
                    
                    try:
                        with zipfile.ZipFile(prev_twbx_path, 'r') as prev_zip:
                            prev_extracted = temp_path / "prev"
                            prev_extracted.mkdir()
                            prev_zip.extractall(prev_extracted)
                            prev_twb_files = list(prev_extracted.glob("*.twb"))
                            if prev_twb_files:
                                prev_source_twb = prev_twb_files[0]
                    except zipfile.BadZipFile:
                        # Not a valid zip, skip
                        pass
                    finally:
                        try:
                            Path(prev_twbx_path).unlink()
                        except:
                            pass
            except Exception as e:
                # If we can't get previous version, that's okay - we'll compare directly
                pass
            
            # If we have previous version, get diff
            if prev_source_twb and prev_source_twb.exists():
                # Get diff between previous and current .twb
                diff_result = subprocess.run(
                    ["git", "diff", "--no-index", str(prev_source_twb), str(source_twb)],
                    cwd=repo_root,
                    capture_output=True,
                    text=True,
                    check=False
                )
                
                if diff_result.stdout.strip() and diff_result.returncode in [0, 1]:
                    # Apply the diff to destination .twb
                    patch_result = apply_twb_diff(prev_source_twb, source_twb, dest_twb, diff_result.stdout)
                    if patch_result != 0:
                        return patch_result
                else:
                    # No changes detected
                    print("✓ No changes detected in .twb file")
            else:
                # No previous version, check if files are different
                import filecmp
                if not filecmp.cmp(source_twb, dest_twb, shallow=False):
                    # Files are different, copy the .twb
                    shutil.copy2(source_twb, dest_twb)
                    print("✓ Updated .twb file in destination")
                else:
                    print("✓ Files are already identical")
            
            # Repackage the .twbx file
            with zipfile.ZipFile(dest_file, 'w', zipfile.ZIP_DEFLATED) as dest_zip:
                for file_path in dest_extracted.rglob('*'):
                    if file_path.is_file():
                        arcname = file_path.relative_to(dest_extracted)
                        dest_zip.write(file_path, arcname)
            
            print(f"✓ Applied changes to {dest_file}")
            return 0
    
    except Exception as e:
        print(f"Error applying .twbx changes: {e}")
        import traceback
        traceback.print_exc()
        return 1

def apply_twb_diff(prev_twb, current_twb, dest_twb, diff_output):
    """Apply diff to .twb file"""
    try:
        # Create a patch file
        with tempfile.NamedTemporaryFile(mode='w', suffix='.patch', delete=False) as patch_file:
            # Modify patch to point to destination
            modified_patch = diff_output.replace(str(current_twb), str(dest_twb))
            modified_patch = modified_patch.replace(str(prev_twb), str(dest_twb))
            patch_file.write(modified_patch)
            patch_path = patch_file.name
        
        try:
            # Try to apply patch
            apply_result = subprocess.run(
                ["git", "apply", "--ignore-whitespace", "--3way", patch_path],
                cwd=dest_twb.parent,
                capture_output=True,
                text=True,
                check=False
            )
            
            if apply_result.returncode == 0:
                print("✓ Applied diff to .twb file")
                return 0
            else:
                # If patch fails, try manual merge
                print("Warning: Patch application failed, trying manual merge...")
                return manual_twb_merge(prev_twb, current_twb, dest_twb)
        
        finally:
            try:
                Path(patch_path).unlink()
            except:
                pass
    
    except Exception as e:
        print(f"Error applying .twb diff: {e}")
        return 1

def manual_twb_merge(prev_twb, current_twb, dest_twb):
    """Manually merge changes in .twb files"""
    try:
        # Read all three versions
        with open(prev_twb, 'r', encoding='utf-8') as f:
            prev_lines = f.readlines()
        with open(current_twb, 'r', encoding='utf-8') as f:
            current_lines = f.readlines()
        with open(dest_twb, 'r', encoding='utf-8') as f:
            dest_lines = f.readlines()
        
        # Find what changed between prev and current
        import difflib
        diff = list(difflib.unified_diff(prev_lines, current_lines, lineterm=''))
        
        # Apply changes to dest
        # This is a simplified approach - for complex cases might need more sophisticated logic
        current_set = set(current_lines)
        prev_set = set(prev_lines)
        dest_set = set(dest_lines)
        
        # Lines added in current (not in prev)
        added_lines = current_set - prev_set
        
        # Lines removed from prev (not in current)
        removed_lines = prev_set - current_set
        
        # Apply: remove deleted lines, add new lines
        result_lines = dest_lines.copy()
        
        # Remove lines that were deleted
        for line in removed_lines:
            while line in result_lines:
                result_lines.remove(line)
        
        # Add new lines (simplified - append, might need better positioning)
        for line in added_lines:
            if line not in result_lines:
                result_lines.append(line)
        
        # Write result
        with open(dest_twb, 'w', encoding='utf-8') as f:
            f.writelines(result_lines)
        
        print("✓ Manually merged changes to .twb file")
        return 0
    
    except Exception as e:
        print(f"Error in manual merge: {e}")
        # Last resort: copy current
        shutil.copy2(current_twb, dest_twb)
        print("⚠ Fallback: Copied current .twb file")
        return 0

if __name__ == "__main__":
    sys.exit(sync_sales_twb())


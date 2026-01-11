#!/usr/bin/env python3
"""
Script to sync Sales.twb from Live/ to Live/Customers/
This script applies only the changes (diff) from the source file to the destination file.
"""

import subprocess
import sys
import tempfile
from pathlib import Path

def sync_sales_twb():
    """Apply changes from Live/Sales.twb to Live/Customers/Sales.twb using git diff"""
    
    # Get the repository root (assuming script is in root)
    repo_root = Path(__file__).parent.absolute()
    
    source_file = repo_root / "Live" / "Sales.twb"
    dest_file = repo_root / "Live" / "Customers" / "Sales.twb"
    
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

if __name__ == "__main__":
    sys.exit(sync_sales_twb())


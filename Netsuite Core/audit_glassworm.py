import os

def scan_for_glassworm(directory="."):
    print(f"Scanning directory: {os.path.abspath(directory)}")
    clean = True
    
    for root, _, files in os.walk(directory):
        for file in files:
            if file.endswith('.py'):
                file_path = os.path.join(root, file)
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        content = f.read()
                        
                        # 1. Check for specific invisible Unicode ranges used by GlassWorm
                        for char in content:
                            if 0xFE00 <= ord(char) <= 0xFE0F or 0xE0100 <= ord(char) <= 0xE01EF:
                                print(f"[!] WARNING: Invisible Unicode variation selector found in: {file_path}")
                                clean = False
                                break
                                
                        # 2. Check for known GlassWorm Python marker variables and C2 addresses
                        if "lzcdrtfxyqiplpd" in content:
                            print(f"[!] MALWARE DETECTED: GlassWorm Python marker variable found in: {file_path}")
                            clean = False
                        
                        if "6YGcuyFRJKZtcaYCCFba9fScNUvPkGXodXE1mJiSzqDJ" in content:
                            print(f"[!] MALWARE DETECTED: GlassWorm Solana C2 wallet address found in: {file_path}")
                            clean = False
                            
                except Exception as e:
                    print(f"Could not read {file_path}: {e}")
                    
    if clean:
        print("Audit Complete: No GlassWorm indicators or hidden Unicode characters found in source files.")

if __name__ == "__main__":
    scan_for_glassworm()
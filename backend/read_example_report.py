"""One-off script to inspect Digital Orders by Platform example Excel."""
import pandas as pd
p = r"c:\Users\KimAylesworth\Downloads\Digital Orders by Platform-Example.xlsx"
xl = pd.ExcelFile(p)
print("Sheets:", xl.sheet_names)
for s in xl.sheet_names:
    df = pd.read_excel(p, sheet_name=s)
    print("---", s, "---")
    print("Columns:", list(df.columns))
    print(df.head(15).to_string())
    print()

import csv
with open("Totals Are Off - Sheet1.csv", "r", encoding="utf-8") as f:
    rows = list(csv.reader(f))

store_totals = []
entree_qtys = []
for row in rows[1:]:
    if len(row) < 10:
        continue
    try:
        st = row[9].strip().replace(",", "")
        if st and st.isdigit():
            store_totals.append(int(st))
    except Exception:
        pass
    if len(row) >= 14 and row[12]:
        try:
            eq = row[13].strip().replace(",", "")
            if eq and eq.isdigit():
                entree_qtys.append(int(eq))
        except Exception:
            pass

sum_stores = sum(store_totals)
sum_entrees = sum(entree_qtys)
print("Sum of store totals (col J):", sum_stores)
print("Sum of entree quantities (col M):", sum_entrees)
print("Difference:", abs(sum_stores - sum_entrees))
print("Store total count:", len(store_totals))
print("Entree rows count:", len(entree_qtys))

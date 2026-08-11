import streamlit as st
import pandas as pd
import re
from difflib import SequenceMatcher
import io

# Konfigurasi Halaman Streamlit
st.set_page_config(
    page_title="Deteksi & Resolusi Data Bayi Ganda",
    page_icon="👶",
    layout="wide"
)

# Daftar kata generik nama orang tua yang diabaikan dalam pencocokan Tier 3
GENERIC_PARENT_NAMES = {
    'ibu', 'mama', 'bapak', 'ayah', 'ortu', 'orang tua', 
    'anonim', 'null', 'none', 'i bu', 'ibuk'
}

# --- FUNGSI HELPER & LOGIKA DETEKSI ---

def clean_text(text):
    if pd.isna(text):
        return ""
    return re.sub(r'[^a-z0-9\s]', '', str(text).lower().strip())

def similarity_score(a, b):
    return SequenceMatcher(None, a, b).ratio()

# 💡 FUNGSI PENILAIAN REKOMENDASI MASTER RECORD
def calculate_score(row, group_df):
    score = 0
    
    # 1. Kelengkapan Imunisasi (Bobot Maksimal 50 Poin)
    imun_cols = [c for c in group_df.columns if 'Tanggal Imunisasi' in c or 'Tanggal IDL' in c]
    filled_imun = row[imun_cols].notnull().sum()
    score += (filled_imun / len(imun_cols)) * 50 if imun_cols else 0
    
    # 2. Keabsahan NIK Anak (Bobot 30 Poin)
    nik = str(row.get('NIK Anak', '')).replace("'", "").strip()
    if len(nik) == 16 and not nik.startswith("033"):
        score += 30
    elif len(nik) == 16:
        score += 15
        
    # 3. Kelengkapan Nama Anak (Bobot 10 Poin)
    nama = str(row.get('Nama Anak', '')).strip()
    if len(nama) > 3 and not nama.lower().startswith("by"):
        score += 10
        
    # 4. Kejelasan Nama Ortu (Bobot 10 Poin)
    ortu = clean_text(row.get('Nama Orang Tua', ''))
    if ortu and ortu not in GENERIC_PARENT_NAMES:
        score += 10
        
    return score

def get_best_option_id(group_df):
    scores = {}
    for idx, row in group_df.iterrows():
        scores[row['ID']] = calculate_score(row, group_df)
    return max(scores, key=scores.get)

# 💡 FUNGSI SMART CHECKBOX PENGGABUNGAN IMUNISASI
def should_suggest_merge(master_id, group_df):
    """Mengecek apakah data duplikat memiliki riwayat imunisasi komplementer untuk mengisi kekosongan Master."""
    master_rows = group_df[group_df['ID'] == master_id]
    if master_rows.empty:
        return True
    
    master_row = master_rows.iloc[0]
    dup_rows = group_df[group_df['ID'] != master_id]
    
    imun_cols = [c for c in group_df.columns if 'Tanggal Imunisasi' in c or 'Tanggal IDL' in c]
    
    for col in imun_cols:
        if pd.isna(master_row[col]) and dup_rows[col].notnull().any():
            return True # Ditemukan data imunisasi tambahan di duplikat
            
    return False


def run_detection(df):
    df = df.copy()
    
    # Konversi ID & NIK Anak ke string bersih untuk cegah PyArrow OverflowError
    if 'ID' in df.columns:
        df['ID'] = df['ID'].astype(str).str.replace(r'\.0$', '', regex=True)
    if 'NIK Anak' in df.columns:
        df['NIK Anak'] = df['NIK Anak'].astype(str).str.replace("'", "").str.strip()
    
    # Preprocessing Teks
    df['nama_anak_clean'] = df['Nama Anak'].apply(clean_text)
    df['ortu_clean'] = df['Nama Orang Tua'].apply(clean_text)
    df['jk_clean'] = df['Jenis Kelamin Anak'].astype(str).str.lower().str.strip() if 'Jenis Kelamin Anak' in df.columns else ""
    
    # Konversi Tanggal Lahir ke string ISO
    df['tgl_lahir_clean'] = pd.to_datetime(df['Tanggal Lahir Anak'], errors='coerce').dt.strftime('%Y-%m-%d')
    
    df['duplicate_tier'] = None
    df['group_id'] = None
    
    group_counter = 1
    processed = set()
    rows = df.to_dict('records')
    n = len(rows)
    
    for i in range(n):
        if i in processed:
            continue
        
        matches = [i]
        tier = None
        
        for j in range(i + 1, n):
            if j in processed:
                continue
                
            # --- TIER 1: Exact NIK Match (NIK Valid 16 Digit) ---
            nik_i = str(rows[i].get('NIK Anak', '')).replace("'", "").strip()
            nik_j = str(rows[j].get('NIK Anak', '')).replace("'", "").strip()
            if len(nik_i) == 16 and len(nik_j) == 16 and nik_i == nik_j:
                matches.append(j)
                tier = "Tier 1: NIK Valid Sama"
                continue
                
            # --- TIER 2: Nama Anak + Tanggal Lahir Sama Persis ---
            if (rows[i]['nama_anak_clean'] == rows[j]['nama_anak_clean'] and 
                rows[i]['tgl_lahir_clean'] == rows[j]['tgl_lahir_clean'] and 
                len(rows[i]['nama_anak_clean']) > 2):
                matches.append(j)
                tier = tier or "Tier 2: Nama & Tgl Lahir Sama"
                continue
                
            # --- TIER 3: Fuzzy Match / Ortu + Tanggal Lahir ---
            if rows[i]['tgl_lahir_clean'] and rows[i]['tgl_lahir_clean'] == rows[j]['tgl_lahir_clean']:
                # Pengecekan Jenis Kelamin (Mencegah kembar beda gender tergabung)
                jk_i = rows[i].get('jk_clean', '')
                jk_j = rows[j].get('jk_clean', '')
                jk_match = (not jk_i or not jk_j or jk_i == jk_j)
                
                if jk_match:
                    sim = similarity_score(rows[i]['nama_anak_clean'], rows[j]['nama_anak_clean'])
                    
                    # 3A. Tanggal lahir sama + Nama Ortu spesifik sama + Kemiripan nama anak >= 50%
                    if (rows[i]['ortu_clean'] and 
                        rows[i]['ortu_clean'] not in GENERIC_PARENT_NAMES and 
                        rows[i]['ortu_clean'] == rows[j]['ortu_clean'] and 
                        sim >= 0.50):
                        matches.append(j)
                        tier = tier or "Tier 3: Ortu & Tgl Lahir Sama"
                        continue
                    
                    # 3B. Tanggal lahir sama + Kemiripan Nama Anak >= 85%
                    if sim >= 0.85 and len(rows[i]['nama_anak_clean']) > 3:
                        matches.append(j)
                        tier = tier or f"Tier 3: Kemiripan Nama ({int(sim*100)}%)"
                        continue

        if len(matches) > 1:
            g_id = f"DUP-{group_counter:04d}"
            for idx in matches:
                df.at[idx, 'duplicate_tier'] = tier
                df.at[idx, 'group_id'] = g_id
                processed.add(idx)
            group_counter += 1
            
    # Hapus kolom pembantu
    cols_to_drop = [c for c in ['nama_anak_clean', 'ortu_clean', 'jk_clean', 'tgl_lahir_clean'] if c in df.columns]
    df = df.drop(columns=cols_to_drop)
    return df


# --- INTERFACE UTAMA STREAMLIT ---

st.title("👶 Aplikasi Deteksi & Resolusi Data Bayi Ganda")
st.markdown("Sistem berbasis web untuk mendeteksi data kohort bayi ganda (Tier 1-3) serta **menggabungkan (merge) atau memverifikasi data** secara interaktif.")

# --- EXPANDER PENJELASAN TIER ---
with st.expander("ℹ️ **Penjelasan Kriteria Deteksi (Tier 1, Tier 2, & Tier 3)**"):
    col_t1, col_t2, col_t3 = st.columns(3)
    
    with col_t1:
        st.markdown("""
        #### 🟢 Tier 1: NIK Valid Sama
        * **Kriteria:** NIK Anak sama persis dan bernilai sah (16 digit angka).
        * **Kepastian:** **Sangat Tinggi**
        """)
        
    with col_t2:
        st.markdown("""
        #### 🟡 Tier 2: Nama & Tgl Lahir Sama
        * **Kriteria:** Tanggal Lahir sama DAN Nama Anak persis.
        * **Kepastian:** **Tinggi**
        """)
        
    with col_t3:
        st.markdown("""
        #### 🟠 Tier 3: Fuzzy / Ortu + Tgl Lahir
        * **Kriteria:** Tanggal Lahir + Jenis Kelamin sama DAN (Nama Ortu spesifik sama & nama anak mirip ≥ 50% OR Kemiripan Nama ≥ 85%).
        * **Kepastian:** **Perlu Verifikasi Manual**
        """)

# --- SIDEBAR UPLOAD & AKSI MASSAL ---
st.sidebar.header("📂 Sumber Data")

# Managing key uploader untuk pengosongan total
if 'uploader_key' not in st.session_state:
    st.session_state['uploader_key'] = 0

uploaded_file = st.sidebar.file_uploader(
    "Unggah File Excel (.xlsx)", 
    type=["xlsx"], 
    key=f"file_uploader_{st.session_state['uploader_key']}"
)

if 'df_working' in st.session_state:
    st.sidebar.divider()
    st.sidebar.header("⚡ Aksi Massal (Bulk Action)")
    
    # 🚀 TOMBOL AUTO-RESOLVE ALL
    if st.session_state.get('processed', False):
        if st.sidebar.button("⚡ Eksekusi Otomatis Semua Sesuai Rekomendasi", type="primary", use_container_width=True):
            df_work = st.session_state['df_working']
            resolved_set = st.session_state.get('resolved_groups', set())
            
            duplicates_df = df_work[df_work['duplicate_tier'].notnull()]
            unresolved_g_ids = [g for g in duplicates_df['group_id'].unique() if g not in resolved_set]
            
            if unresolved_g_ids:
                indices_to_drop = []
                
                with st.spinner(f"Memproses {len(unresolved_g_ids)} kelompok ganda secara otomatis..."):
                    for g_id in unresolved_g_ids:
                        group_data = df_work[df_work['group_id'] == g_id]
                        best_master_id = get_best_option_id(group_data)
                        
                        master_idx = group_data[group_data['ID'] == best_master_id].index[0]
                        duplicate_indices = group_data[group_data['ID'] != best_master_id].index.tolist()
                        
                        # Gabungkan riwayat imunisasi jika ada data komplementer
                        if should_suggest_merge(best_master_id, group_data):
                            for dup_idx in duplicate_indices:
                                for col in df_work.columns:
                                    if pd.isna(df_work.at[master_idx, col]) and pd.notna(df_work.at[dup_idx, col]):
                                        df_work.at[master_idx, col] = df_work.at[dup_idx, col]
                        
                        indices_to_drop.extend(duplicate_indices)
                        resolved_set.add(g_id)
                    
                    df_work = df_work.drop(index=indices_to_drop)
                    st.session_state['df_working'] = df_work
                    st.session_state['resolved_groups'] = resolved_set
                    st.sidebar.success(f"Berhasil menyelesaikan {len(unresolved_g_ids)} kelompok ganda!")
                    st.rerun()

    # 🔴 TOMBOL RESET DENGAN PEMBERSIHAN FILE UPLOAD TOTAL
    if st.sidebar.button("🔄 Reset / Bersihkan Transaksi", type="secondary", use_container_width=True):
        st.session_state.clear()
        st.session_state['uploader_key'] += 1  # Mengganti key uploader agar memicu re-render widget kosong
        st.rerun()

if uploaded_file is not None and 'df_working' not in st.session_state:
    df_raw = pd.read_excel(uploaded_file)
    st.session_state['df_working'] = df_raw.copy()
    st.session_state['processed'] = False
    st.session_state['resolved_groups'] = set()

if 'df_working' in st.session_state:
    df_working = st.session_state['df_working']
    
    st.info(f"📁 **File Aktif:** Total Data: **{len(df_working)}** baris")
    
    if not st.session_state.get('processed', False):
        if st.button("🚀 Jalankan Deteksi Duplikat", type="primary"):
            with st.spinner("Memproses algoritma deteksi ganda Tier 1, 2, dan 3..."):
                result_df = run_detection(df_working)
                st.session_state['df_working'] = result_df
                st.session_state['processed'] = True
                st.rerun()

if st.session_state.get('processed', False):
    df_working = st.session_state['df_working']
    resolved_groups = st.session_state.get('resolved_groups', set())
    
    duplicates = df_working[df_working['duplicate_tier'].notnull()]
    active_duplicates = duplicates[~duplicates['group_id'].isin(resolved_groups)]
    
    tab1, tab2 = st.tabs(["📊 Dashboard & Hasil Deteksi", "🛠️ Modul Resolusi & Merge Data"])
    
    # ---------------- TAB 1: DASHBOARD ----------------
    with tab1:
        st.subheader("Ringkasan Analisis")
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Total Data", f"{len(df_working)} baris")
        col2.metric("Total Terindikasi Ganda", f"{len(duplicates)} baris")
        col3.metric("Kelompok Belum Di-review", f"{active_duplicates['group_id'].nunique()} kelompok")
        col4.metric("Kelompok Terselesaikan", f"{len(resolved_groups)} kelompok")
        
        st.markdown("---")
        
        st.markdown("#### 📌 Rincian Deteksi per Tingkatan (Tier)")
        t1_rows = len(duplicates[duplicates['duplicate_tier'].str.contains("Tier 1", na=False)])
        t1_groups = duplicates[duplicates['duplicate_tier'].str.contains("Tier 1", na=False)]['group_id'].nunique()
        
        t2_rows = len(duplicates[duplicates['duplicate_tier'].str.contains("Tier 2", na=False)])
        t2_groups = duplicates[duplicates['duplicate_tier'].str.contains("Tier 2", na=False)]['group_id'].nunique()
        
        t3_rows = len(duplicates[duplicates['duplicate_tier'].str.contains("Tier 3", na=False)])
        t3_groups = duplicates[duplicates['duplicate_tier'].str.contains("Tier 3", na=False)]['group_id'].nunique()
        
        col_m1, col_m2, col_m3 = st.columns(3)
        col_m1.metric("🟢 Tier 1 (NIK Valid)", f"{t1_groups} kelompok", delta=f"{t1_rows} baris data", delta_color="normal")
        col_m2.metric("🟡 Tier 2 (Nama & Tgl Lahir)", f"{t2_groups} kelompok", delta=f"{t2_rows} baris data", delta_color="normal")
        col_m3.metric("🟠 Tier 3 (Fuzzy / Ortu)", f"{t3_groups} kelompok", delta=f"{t3_rows} baris data", delta_color="normal")
        
        st.divider()
        
        tiers_available = ["Semua Tier"] + list(duplicates['duplicate_tier'].unique())
        selected_tier = st.selectbox("Saring berdasarkan Tingkatan (Tier):", tiers_available)
        
        filtered_dup = duplicates if selected_tier == "Semua Tier" else duplicates[duplicates['duplicate_tier'] == selected_tier]
        cols_to_display = ['group_id', 'duplicate_tier', 'ID', 'NIK Anak', 'Nama Anak', 'Tanggal Lahir Anak', 'Nama Orang Tua', 'Puskesmas']
        
        st.dataframe(
            filtered_dup[cols_to_display].sort_values(by=['group_id', 'duplicate_tier']),
            use_container_width=True,
            hide_index=True
        )

    # ---------------- TAB 2: RESOLUSI & MERGE ----------------
    with tab2:
        st.subheader("🛠️ Modul Penggabungan (Merge) & Eliminasi Duplikat")
        
        unresolved_groups = sorted(active_duplicates['group_id'].unique().tolist())
        
        if not unresolved_groups:
            st.success("🎉 Semua kelompok data ganda telah berhasil diselesaikan/di-review!")
        else:
            selected_group = st.selectbox("Pilih Kelompok Duplikat (Group ID):", unresolved_groups)
            
            group_data = df_working[df_working['group_id'] == selected_group].copy()
            tier_info = group_data['duplicate_tier'].iloc[0]
            
            # Hitung Rekomendasi Terbaik dari Sistem
            recommended_id = get_best_option_id(group_data)
            
            st.info(f"**Tipe Duplikat:** `{tier_info}` | Jumlah Entri: **{len(group_data)}**")
            
            # Tampilkan Perbandingan Side-by-Side dengan Badge Rekomendasi
            cols = st.columns(len(group_data))
            for idx, (index_row, row) in enumerate(group_data.iterrows()):
                with cols[idx]:
                    if str(row['ID']) == str(recommended_id):
                        st.success("⭐ **REKOMENDASI SISTEM**")
                    else:
                        st.markdown("---")
                    
                    st.markdown(f"### Option {idx + 1}")
                    st.write(f"**ID:** {row['ID']}")
                    st.write(f"**NIK Anak:** {row['NIK Anak']}")
                    st.write(f"**Nama Anak:** {row['Nama Anak']}")
                    st.write(f"**Tgl Lahir:** {row['Tanggal Lahir Anak']}")
                    st.write(f"**Nama Ortu:** {row['Nama Orang Tua']}")
                    st.write(f"**Puskesmas:** {row['Puskesmas']}")
                    
                    imun_cols = [c for c in group_data.columns if 'Tanggal Imunisasi' in c or 'Tanggal IDL' in c]
                    filled_imun = row[imun_cols].notnull().sum()
                    st.caption(f"💉 Riwayat Imunisasi Terisi: **{filled_imun} / {len(imun_cols)}**")
            
            st.divider()
            
            # Form Konfigurasi (Rekomendasi Otomatis sebagai Default Pilihan)
            st.write("#### Konfigurasi Penggabungan Data")
            
            master_id_options = group_data['ID'].tolist()
            default_idx = master_id_options.index(recommended_id) if recommended_id in master_id_options else 0
            
            selected_master_id = st.selectbox(
                "Pilih ID yang dijadikan DATA UTAMA (Master Record):",
                master_id_options,
                index=default_idx,
                format_func=lambda x: f"ID: {x} - {group_data[group_data['ID']==x]['Nama Anak'].values[0]}" + (" ⭐ (Rekomendasi)" if str(x) == str(recommended_id) else "")
            )
            
            # 💡 Smart Checkbox Value berdasarkan analisis ketersediaan data komplementer
            suggested_check = should_suggest_merge(selected_master_id, group_data)
            
            merge_imunization = st.checkbox(
                "Otomatis gabungkan riwayat imunisasi yang kosong di Data Utama dari data duplikatnya", 
                value=suggested_check,
                help="Sistem secara otomatis menyarankan centang jika data duplikat memiliki riwayat imunisasi tambahan yang belum terisi di Data Utama."
            )
            
            if suggested_check:
                st.caption("💡 **Saran Sistem:** Dicentang karena terdapat data riwayat imunisasi tambahan pada entri duplikat yang dapat melengkapi Data Utama.")
            
            st.write("")
            btn_col1, btn_col2 = st.columns([1, 1])
            
            # Tombol 1: Selesaikan & Gabung
            with btn_col1:
                if st.button("✅ Selesaikan & Gabungkan Data Kelompok Ini", type="primary", use_container_width=True):
                    master_idx = group_data[group_data['ID'] == selected_master_id].index[0]
                    duplicate_indices = group_data[group_data['ID'] != selected_master_id].index.tolist()
                    
                    if merge_imunization:
                        for dup_idx in duplicate_indices:
                            for col in df_working.columns:
                                if pd.isna(df_working.at[master_idx, col]) and pd.notna(df_working.at[dup_idx, col]):
                                    df_working.at[master_idx, col] = df_working.at[dup_idx, col]
                    
                    df_working = df_working.drop(index=duplicate_indices)
                    st.session_state['resolved_groups'].add(selected_group)
                    st.session_state['df_working'] = df_working
                    st.success(f"Kelompok {selected_group} berhasil digabungkan ke ID {selected_master_id}!")
                    st.rerun()

            # Tombol 2: Lewati
            with btn_col2:
                if st.button("⏭️ Lewati (Bukan Duplikat)", type="secondary", use_container_width=True):
                    st.session_state['resolved_groups'].add(selected_group)
                    st.info(f"Kelompok {selected_group} dilewati. Kedua data dipertahankan secara terpisah.")
                    st.rerun()

    # ---------------- EXPORT CLEAN DATASET ----------------
    st.sidebar.divider()
    st.sidebar.header("💾 Export Data Bersih")
    
    clean_df = df_working.drop(columns=['duplicate_tier', 'group_id'], errors='ignore')
    
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        clean_df.to_excel(writer, index=False, sheet_name='Kohort_Clean')
    processed_data = output.getvalue()
    
    st.sidebar.download_button(
        label="📥 Unduh Data Bersih (.xlsx)",
        data=processed_data,
        file_name="Data_Kohort_Bayi_Clean.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
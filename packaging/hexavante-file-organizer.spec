# hexavante-file-organizer.spec — Sprint 07 (preparação, sem build privilegiado)
#
# Pacote INTERNO Hexavante v0.1 (License: Proprietary — NÃO submetido ao
# Fedora oficial; destinado a COPR interno ou `rpmbuild` local).
# Build validado em host Fedora com:
#   rpmbuild -ba packaging/hexavante-file-organizer.spec   # requer rpm-build + python3-devel (NÃO executado neste CI sem privilégios)
# Pré-condição de segurança: o %install nunca toca em ~ ou /home; o app
# em runtime só escreve no XDG do usuário e no SQLite do próprio usuário.

Name:           hexavante-file-organizer
Version:        0.1.0
Release:        1%{?dist}
Summary:        Organizador seguro de arquivos (desktop Fedora, GTK4, offline)
License:        Proprietary
URL:            https://github.com/nicolaskmazzini-coder/Organizador-Pessoal
Source0:        %{name}-%{version}.tar.gz

BuildArch:      noarch
BuildRequires:  python3-devel
BuildRequires:  python3-setuptools
BuildRequires:  python3-pytest
Requires:       python3-gobject
Requires:       gtk4
Requires:       libadwaita
Requires:       python3-sqlite3-default

%description
Aplicativo desktop nativo Fedora (GTK4/Libadwaita, offline) para reconhecer
pastas existentes e sugerir a organização segura de arquivos. Núcleo Python
puro testável sem display; movimentação somente com aprovação explícita
vinculada ao hash do plano, journal SQLite, crash recovery e undo.

%prep
%autosetup -n %{name}-%{version}

%build
%pyproject_wheel

%install
%pyproject_install
install -Dm644 docs/ARCHITECTURE.md %{buildroot}%{_docdir}/%{name}/ARCHITECTURE.md
install -Dm644 docs/SECURITY.md %{buildroot}%{_docdir}/%{name}/SECURITY.md
install -Dm644 docs/SPRINT_STATUS.md %{buildroot}%{_docdir}/%{name}/SPRINT_STATUS.md

%check
%{python3} -m pytest -q

%files
%{python3_sitelib}/hexavante/
%{_bindir}/hexavante
%{_docdir}/%{name}/

%changelog
* Mon Oct 12 2026 Hexavante <dev@hexavante.local> - 0.1.0-1
- Sprint 07: v0.1 candidata — fundação, scanner, classificação, movimentação
  segura, UI nativa, duplicados+monitor (só sugestões), agendamentos dry-run.

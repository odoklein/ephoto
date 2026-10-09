<?php
/**
 * Admin interface and native review dashboard for Certif ID Ephoto.
 *
 * Every AJAX handler checks the nonce AND the capability, and reads the
 * submission id from the order meta (never from the request).
 */

if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

class Certif_Ephoto_Admin {

	const SLUG = 'certif-ephoto-control';

	const AJAX_NONCE  = 'certif_ephoto_ajax_nonce';
	const IMAGE_NONCE = 'certif_ephoto_image';

	public static function init() {
		add_action( 'admin_menu', array( __CLASS__, 'register_menus' ) );
		add_action( 'admin_enqueue_scripts', array( __CLASS__, 'enqueue_assets' ) );

		// AJAX endpoints (logged-in users only).
		add_action( 'wp_ajax_certif_get_order_review', array( __CLASS__, 'ajax_get_order_review' ) );
		add_action( 'wp_ajax_certif_validate_order', array( __CLASS__, 'ajax_validate_order' ) );
		add_action( 'wp_ajax_certif_recrop_photo', array( __CLASS__, 'ajax_recrop_photo' ) );
		add_action( 'wp_ajax_certif_rotate_signature', array( __CLASS__, 'ajax_rotate_signature' ) );
		add_action( 'wp_ajax_certif_sync_order', array( __CLASS__, 'ajax_sync_order' ) );
		add_action( 'wp_ajax_certif_ephoto_image', array( __CLASS__, 'ajax_image' ) );
	}

	public static function capability() {
		$default = class_exists( 'WooCommerce' ) ? 'manage_woocommerce' : 'manage_options';
		return apply_filters( 'certif_ephoto_capability', $default );
	}

	/**
	 * Labels for the local dossier statuses.
	 */
	public static function status_labels() {
		return array(
			'to_send'    => __( 'À analyser', 'certif-ephoto-control' ),
			'processing' => __( 'Traitement en cours', 'certif-ephoto-control' ),
			'pending'    => __( 'Prêt à contrôler', 'certif-ephoto-control' ),
			'error'      => __( 'Erreur de traitement', 'certif-ephoto-control' ),
			'accepted'   => __( 'Transmis', 'certif-ephoto-control' ),
			'rejected'   => __( 'Refusé', 'certif-ephoto-control' ),
			'missing'    => __( 'Introuvable sur le service', 'certif-ephoto-control' ),
		);
	}

	public static function status_label( $status ) {
		$labels = self::status_labels();
		return isset( $labels[ $status ] ) ? $labels[ $status ] : $labels['to_send'];
	}

	/**
	 * URL of the review screen for one order (view only, never ingests).
	 */
	public static function review_url( $order_id, $open_modal = true ) {
		$args = array(
			'page' => self::SLUG,
			'tab'  => 'all',
			's'    => absint( $order_id ),
		);
		if ( $open_modal ) {
			$args['order_id'] = absint( $order_id );
		}
		return add_query_arg( $args, admin_url( 'admin.php' ) );
	}

	public static function register_menus() {
		$cap = self::capability();

		add_menu_page(
			__( 'Contrôle photos', 'certif-ephoto-control' ),
			__( 'Contrôle photos', 'certif-ephoto-control' ),
			$cap,
			self::SLUG,
			array( __CLASS__, 'render_dashboard' ),
			'dashicons-id-alt',
			56
		);

		add_submenu_page(
			self::SLUG,
			__( 'Dossiers à contrôler', 'certif-ephoto-control' ),
			__( 'Dossiers', 'certif-ephoto-control' ),
			$cap,
			self::SLUG,
			array( __CLASS__, 'render_dashboard' )
		);

		add_submenu_page(
			self::SLUG,
			__( 'Réglages – Contrôle photos', 'certif-ephoto-control' ),
			__( 'Réglages', 'certif-ephoto-control' ),
			'manage_options',
			Certif_Ephoto_Settings::PAGE,
			array( 'Certif_Ephoto_Settings', 'render' )
		);
	}

	public static function enqueue_assets( $hook ) {
		if ( false === strpos( (string) $hook, self::SLUG ) ) {
			return;
		}

		wp_enqueue_style(
			'certif-ephoto-admin',
			plugins_url( 'assets/css/admin.css', CERTIF_EPHOTO_FILE ),
			array(),
			CERTIF_EPHOTO_VERSION
		);

		wp_enqueue_script(
			'certif-ephoto-admin',
			plugins_url( 'assets/js/admin.js', CERTIF_EPHOTO_FILE ),
			array( 'jquery' ),
			CERTIF_EPHOTO_VERSION,
			true
		);

		wp_localize_script(
			'certif-ephoto-admin',
			'certifEphoto',
			array(
				'ajaxUrl'      => admin_url( 'admin-ajax.php' ),
				'nonce'        => wp_create_nonce( self::AJAX_NONCE ),
				'imageNonce'   => wp_create_nonce( self::IMAGE_NONCE ),
				'statusLabels' => self::status_labels(),
				'i18n'         => array(
					'loading'           => __( 'Chargement...', 'certif-ephoto-control' ),
					'sending'           => __( 'Envoi en cours...', 'certif-ephoto-control' ),
					'confirmAccept'     => __( 'Confirmer la validation du dossier et sa transmission à Make / ePhoto ?', 'certif-ephoto-control' ),
					'rejectPrompt'      => __( 'Motif du refus (obligatoire) :', 'certif-ephoto-control' ),
					'rejectEmpty'       => __( 'Le motif du refus est obligatoire.', 'certif-ephoto-control' ),
					'errorOccurred'     => __( 'Une erreur est survenue.', 'certif-ephoto-control' ),
					'sessionExpired'    => __( 'Session expirée ou accès refusé : rechargez la page.', 'certif-ephoto-control' ),
					'acceptedSuccess'   => __( 'Dossier accepté et transmis.', 'certif-ephoto-control' ),
					'rejectedSuccess'   => __( 'Dossier refusé.', 'certif-ephoto-control' ),
					'titlePrefix'       => __( 'Examen du dossier — Commande #', 'certif-ephoto-control' ),
					'processing'        => __( 'Traitement en cours par le service… actualisation automatique toutes les 3 secondes.', 'certif-ephoto-control' ),
					'processingTimeout' => __( 'Le traitement prend plus de temps que prévu. Fermez puis rouvrez le dossier dans quelques minutes.', 'certif-ephoto-control' ),
					'notReady'          => __( 'Ce dossier n’est pas prêt à être accepté (fichier manquant ou analyse incomplète). Ajustez-le, relancez l’analyse ou refusez-le.', 'certif-ephoto-control' ),
					'readyToReview'     => __( 'Prêt à contrôler.', 'certif-ephoto-control' ),
					'errorStatus'       => __( 'Erreur de traitement :', 'certif-ephoto-control' ),
					'acceptedStatus'    => __( 'Dossier accepté', 'certif-ephoto-control' ),
					'rejectedStatus'    => __( 'Dossier refusé', 'certif-ephoto-control' ),
					'decidedOn'         => __( 'le', 'certif-ephoto-control' ),
					'reasonLabel'       => __( 'Motif :', 'certif-ephoto-control' ),
					'forwardLabel'      => __( 'Transmission :', 'certif-ephoto-control' ),
					'checkPass'         => __( '✔ Conforme', 'certif-ephoto-control' ),
					'checkFail'         => __( '✘ Écart', 'certif-ephoto-control' ),
					'checkUnknown'      => __( '— À vérifier', 'certif-ephoto-control' ),
					'noChecks'          => __( 'Aucun contrôle disponible.', 'certif-ephoto-control' ),
					'invalidAngle'      => __( 'Angle invalide (entre -360 et 360).', 'certif-ephoto-control' ),
				),
			)
		);
	}

	/**
	 * Render the review dashboard.
	 */
	public static function render_dashboard() {
		if ( ! current_user_can( self::capability() ) ) {
			wp_die( esc_html__( 'Accès refusé.', 'certif-ephoto-control' ), '', array( 'response' => 403 ) );
		}

		try {
			$current_tab = isset( $_GET['tab'] ) && is_string( $_GET['tab'] ) ? sanitize_key( wp_unslash( $_GET['tab'] ) ) : 'open'; // phpcs:ignore WordPress.Security.NonceVerification.Recommended
			if ( ! in_array( $current_tab, array( 'open', 'accepted', 'rejected', 'all' ), true ) ) {
				$current_tab = 'open';
			}
			$search_query = isset( $_GET['s'] ) && is_string( $_GET['s'] ) ? sanitize_text_field( wp_unslash( $_GET['s'] ) ) : ''; // phpcs:ignore WordPress.Security.NonceVerification.Recommended

			$query_args = array(
				'limit'   => 50,
				'orderby' => 'date',
				'order'   => 'DESC',
				'return'  => 'ids',
				'type'    => 'shop_order',
			);

			if ( '' !== $search_query ) {
				if ( preg_match( '/^\d+$/', $search_query ) ) {
					$query_args['include'] = array( absint( $search_query ) );
				} else {
					$query_args['customer'] = $search_query;
				}
			}

			$order_ids = function_exists( 'wc_get_orders' ) ? wc_get_orders( $query_args ) : array();

			$count_open     = 0;
			$count_accepted = 0;
			$count_rejected = 0;
			$count_all      = 0;
			$records        = array();

			foreach ( $order_ids as $oid ) {
				$order = wc_get_order( $oid );
				if ( ! $order instanceof WC_Order ) {
					continue;
				}

				$sub_id = Certif_Ephoto_Client::clean_submission_id( $order->get_meta( '_certif_ephoto_submission_id' ) );
				$status = (string) $order->get_meta( '_certif_ephoto_status' );
				$data   = Certif_Ephoto_Order_Reader::extract_dossier_data( $order );
				if ( is_wp_error( $data ) ) {
					continue;
				}

				// Only orders with ePhoto files or a submission.
				if ( '' === $sub_id && empty( $data['photo_url'] ) && empty( $data['signature_url'] ) ) {
					continue;
				}

				if ( '' === $sub_id ) {
					$status = 'to_send';
				} elseif ( '' === $status || ! array_key_exists( $status, self::status_labels() ) ) {
					$status = 'processing';
				}

				$count_all++;
				if ( 'accepted' === $status ) {
					$count_accepted++;
				} elseif ( 'rejected' === $status ) {
					$count_rejected++;
				} else {
					$count_open++;
				}

				if ( 'open' === $current_tab && ( 'accepted' === $status || 'rejected' === $status ) ) {
					continue;
				}
				if ( 'accepted' === $current_tab && 'accepted' !== $status ) {
					continue;
				}
				if ( 'rejected' === $current_tab && 'rejected' !== $status ) {
					continue;
				}

				$records[] = array(
					'order_id'      => $order->get_id(),
					'order'         => $order,
					'submission_id' => $sub_id,
					'status'        => $status,
					'data'          => $data,
				);
			}

			$base_url = admin_url( 'admin.php?page=' . self::SLUG );
			?>
		<div class="wrap certif-dashboard-wrap">
			<div class="certif-header">
				<div>
					<span class="certif-eyebrow"><?php esc_html_e( 'Contrôle & Validation ANTS', 'certif-ephoto-control' ); ?></span>
					<h1 class="certif-title"><?php esc_html_e( 'Dossiers ePhoto WooCommerce', 'certif-ephoto-control' ); ?></h1>
					<p class="certif-subtitle"><?php esc_html_e( 'Vérifiez les photos et signatures de vos commandes, puis validez pour transmettre à ePhoto.', 'certif-ephoto-control' ); ?></p>
				</div>
				<?php if ( current_user_can( 'manage_options' ) ) : ?>
					<div class="certif-header-actions">
						<a href="<?php echo esc_url( admin_url( 'admin.php?page=' . Certif_Ephoto_Settings::PAGE ) ); ?>" class="button">
							⚙ <?php esc_html_e( 'Réglages', 'certif-ephoto-control' ); ?>
						</a>
					</div>
				<?php endif; ?>
			</div>

			<div class="certif-stats-grid">
				<a href="<?php echo esc_url( add_query_arg( 'tab', 'open', $base_url ) ); ?>"
					class="certif-stat-card <?php echo 'open' === $current_tab ? 'active-open' : ''; ?>">
					<div class="stat-top">
						<span class="stat-label"><?php esc_html_e( 'À traiter', 'certif-ephoto-control' ); ?></span>
						<span class="stat-indicator pulse-yellow"></span>
					</div>
					<div class="stat-number"><?php echo esc_html( $count_open ); ?></div>
					<div class="stat-desc"><?php esc_html_e( 'À analyser, en traitement ou prêts à contrôler', 'certif-ephoto-control' ); ?></div>
				</a>

				<a href="<?php echo esc_url( add_query_arg( 'tab', 'accepted', $base_url ) ); ?>"
					class="certif-stat-card <?php echo 'accepted' === $current_tab ? 'active-accepted' : ''; ?>">
					<div class="stat-top">
						<span class="stat-label"><?php esc_html_e( 'Transmis', 'certif-ephoto-control' ); ?></span>
						<span class="stat-indicator green"></span>
					</div>
					<div class="stat-number"><?php echo esc_html( $count_accepted ); ?></div>
					<div class="stat-desc"><?php esc_html_e( 'Acceptés et envoyés à Make / ePhoto', 'certif-ephoto-control' ); ?></div>
				</a>

				<a href="<?php echo esc_url( add_query_arg( 'tab', 'rejected', $base_url ) ); ?>"
					class="certif-stat-card <?php echo 'rejected' === $current_tab ? 'active-rejected' : ''; ?>">
					<div class="stat-top">
						<span class="stat-label"><?php esc_html_e( 'Refusés', 'certif-ephoto-control' ); ?></span>
						<span class="stat-indicator red"></span>
					</div>
					<div class="stat-number"><?php echo esc_html( $count_rejected ); ?></div>
					<div class="stat-desc"><?php esc_html_e( 'Exceptions ou photos non conformes', 'certif-ephoto-control' ); ?></div>
				</a>

				<a href="<?php echo esc_url( add_query_arg( 'tab', 'all', $base_url ) ); ?>"
					class="certif-stat-card <?php echo 'all' === $current_tab ? 'active-all' : ''; ?>">
					<div class="stat-top">
						<span class="stat-label"><?php esc_html_e( 'Total dossiers', 'certif-ephoto-control' ); ?></span>
					</div>
					<div class="stat-number"><?php echo esc_html( $count_all ); ?></div>
					<div class="stat-desc"><?php esc_html_e( 'Parmi les 50 dernières commandes', 'certif-ephoto-control' ); ?></div>
				</a>
			</div>

			<div class="certif-table-container">
				<div class="certif-table-toolbar">
					<nav class="certif-tab-nav">
						<a href="<?php echo esc_url( add_query_arg( 'tab', 'open', $base_url ) ); ?>" class="tab-link <?php echo 'open' === $current_tab ? 'active' : ''; ?>">
							<?php esc_html_e( 'À traiter', 'certif-ephoto-control' ); ?> <span class="badge"><?php echo esc_html( $count_open ); ?></span>
						</a>
						<a href="<?php echo esc_url( add_query_arg( 'tab', 'accepted', $base_url ) ); ?>" class="tab-link <?php echo 'accepted' === $current_tab ? 'active' : ''; ?>">
							<?php esc_html_e( 'Transmis', 'certif-ephoto-control' ); ?> <span class="badge"><?php echo esc_html( $count_accepted ); ?></span>
						</a>
						<a href="<?php echo esc_url( add_query_arg( 'tab', 'rejected', $base_url ) ); ?>" class="tab-link <?php echo 'rejected' === $current_tab ? 'active' : ''; ?>">
							<?php esc_html_e( 'Refusés', 'certif-ephoto-control' ); ?> <span class="badge"><?php echo esc_html( $count_rejected ); ?></span>
						</a>
						<a href="<?php echo esc_url( add_query_arg( 'tab', 'all', $base_url ) ); ?>" class="tab-link <?php echo 'all' === $current_tab ? 'active' : ''; ?>">
							<?php esc_html_e( 'Toutes', 'certif-ephoto-control' ); ?> <span class="badge"><?php echo esc_html( $count_all ); ?></span>
						</a>
					</nav>

					<form method="get" class="certif-search-form">
						<input type="hidden" name="page" value="<?php echo esc_attr( self::SLUG ); ?>">
						<input type="hidden" name="tab" value="<?php echo esc_attr( $current_tab ); ?>">
						<input type="search" name="s" value="<?php echo esc_attr( $search_query ); ?>" placeholder="<?php esc_attr_e( 'N° de commande ou e-mail client', 'certif-ephoto-control' ); ?>" class="certif-search-input">
						<button type="submit" class="button"><?php esc_html_e( 'Rechercher', 'certif-ephoto-control' ); ?></button>
					</form>
				</div>

				<table class="wp-list-table widefat fixed striped certif-orders-table">
					<thead>
						<tr>
							<th style="width:140px;"><?php esc_html_e( 'Commande', 'certif-ephoto-control' ); ?></th>
							<th><?php esc_html_e( 'Client', 'certif-ephoto-control' ); ?></th>
							<th style="width:170px;"><?php esc_html_e( 'Aperçus', 'certif-ephoto-control' ); ?></th>
							<th style="width:180px;"><?php esc_html_e( 'Statut', 'certif-ephoto-control' ); ?></th>
							<th style="width:200px;text-align:right;"><?php esc_html_e( 'Action', 'certif-ephoto-control' ); ?></th>
						</tr>
					</thead>
					<tbody>
						<?php if ( empty( $records ) ) : ?>
							<tr>
								<td colspan="5" class="certif-empty-state">
									<p><?php esc_html_e( 'Aucun dossier dans cette sélection.', 'certif-ephoto-control' ); ?></p>
								</td>
							</tr>
						<?php else : ?>
							<?php
							foreach ( $records as $rec ) :
								$order  = $rec['order'];
								$data   = $rec['data'];
								$oid    = $rec['order_id'];
								$st     = $rec['status'];
								$sub_id = $rec['submission_id'];
								?>
								<tr id="certif-row-<?php echo esc_attr( $oid ); ?>" class="certif-row status-<?php echo esc_attr( $st ); ?>" data-order-id="<?php echo esc_attr( $oid ); ?>" data-status="<?php echo esc_attr( $st ); ?>">
									<td class="col-order">
										<a href="<?php echo esc_url( $order->get_edit_order_url() ); ?>" class="order-link">
											<strong>#<?php echo esc_html( $order->get_order_number() ); ?></strong>
										</a>
										<div class="order-date"><?php echo esc_html( $order->get_date_created() ? $order->get_date_created()->date_i18n( 'd/m/Y H:i' ) : '' ); ?></div>
										<span class="wc-status wc-status-<?php echo esc_attr( $order->get_status() ); ?>">
											<?php echo esc_html( wc_get_order_status_name( $order->get_status() ) ); ?>
										</span>
									</td>

									<td class="col-customer">
										<div class="customer-name"><?php echo esc_html( $data['customer']['name'] ); ?></div>
										<?php if ( '' !== $data['customer']['email'] ) : ?>
											<div class="customer-email"><a href="<?php echo esc_url( 'mailto:' . $data['customer']['email'] ); ?>"><?php echo esc_html( $data['customer']['email'] ); ?></a></div>
										<?php endif; ?>
										<?php if ( '' !== $sub_id ) : ?>
											<span class="sub-id">ID : <code><?php echo esc_html( substr( $sub_id, 0, 8 ) ); ?></code></span>
										<?php endif; ?>
									</td>

									<td class="col-previews">
										<div class="previews-wrap">
											<?php if ( ! empty( $data['photo_url'] ) ) : ?>
												<a href="<?php echo esc_url( $data['photo_url'] ); ?>" target="_blank" rel="noopener noreferrer" class="thumb-link" title="<?php esc_attr_e( 'Photo originale', 'certif-ephoto-control' ); ?>">
													<img src="<?php echo esc_url( $data['photo_url'] ); ?>" class="thumb-photo" alt="<?php esc_attr_e( 'Photo', 'certif-ephoto-control' ); ?>" loading="lazy" referrerpolicy="no-referrer">
												</a>
											<?php endif; ?>
											<?php if ( ! empty( $data['signature_url'] ) ) : ?>
												<a href="<?php echo esc_url( $data['signature_url'] ); ?>" target="_blank" rel="noopener noreferrer" class="thumb-link" title="<?php esc_attr_e( 'Signature originale', 'certif-ephoto-control' ); ?>">
													<img src="<?php echo esc_url( $data['signature_url'] ); ?>" class="thumb-signature" alt="<?php esc_attr_e( 'Signature', 'certif-ephoto-control' ); ?>" loading="lazy" referrerpolicy="no-referrer">
												</a>
											<?php endif; ?>
										</div>
									</td>

									<td class="col-status">
										<span class="badge-status <?php echo esc_attr( $st ); ?>"><?php echo esc_html( self::status_label( $st ) ); ?></span>
									</td>

									<td class="col-actions" style="text-align:right;">
										<div class="certif-row-actions">
											<?php if ( '' !== $sub_id ) : ?>
												<button type="button" class="button button-primary btn-examine" data-order-id="<?php echo esc_attr( $oid ); ?>">
													<?php esc_html_e( 'Examiner', 'certif-ephoto-control' ); ?> →
												</button>
												<?php if ( in_array( $st, array( 'rejected', 'error', 'missing' ), true ) ) : ?>
													<button type="button" class="button button-secondary btn-sync" data-order-id="<?php echo esc_attr( $oid ); ?>"
														data-confirm="<?php esc_attr_e( 'Relancer l’analyse créera un nouveau dossier sur le service. Continuer ?', 'certif-ephoto-control' ); ?>">
														<?php esc_html_e( 'Relancer l’analyse', 'certif-ephoto-control' ); ?>
													</button>
												<?php endif; ?>
											<?php else : ?>
												<button type="button" class="button button-secondary btn-sync" data-order-id="<?php echo esc_attr( $oid ); ?>">
													<?php esc_html_e( 'Lancer l’analyse', 'certif-ephoto-control' ); ?>
												</button>
											<?php endif; ?>
										</div>
									</td>
								</tr>
							<?php endforeach; ?>
						<?php endif; ?>
					</tbody>
				</table>
			</div>
		</div>

		<!-- Review Modal -->
		<div id="certif-review-modal" class="certif-modal" style="display:none;">
			<div class="certif-modal-backdrop"></div>
			<div class="certif-modal-dialog" role="dialog" aria-modal="true" aria-labelledby="modal-order-title">
				<div class="certif-modal-content">
					<header class="certif-modal-header">
						<div class="modal-header-info">
							<h2 id="modal-order-title"><?php esc_html_e( 'Examen du dossier', 'certif-ephoto-control' ); ?></h2>
							<span id="modal-customer-info" class="modal-subheading"></span>
						</div>
						<button type="button" class="certif-modal-close" aria-label="<?php esc_attr_e( 'Fermer', 'certif-ephoto-control' ); ?>">&times;</button>
					</header>

					<div class="certif-modal-body">
						<div id="modal-loading-indicator" class="modal-loading">
							<span class="spinner is-active" style="float:none;"></span>
							<p><?php esc_html_e( 'Chargement du contrôle de conformité ANTS...', 'certif-ephoto-control' ); ?></p>
						</div>

						<div id="modal-error-box" class="certif-modal-banner banner-error" style="display:none;"></div>
						<div id="modal-status-banner" class="certif-modal-banner" style="display:none;"></div>

						<div id="modal-dossier-content" class="modal-dossier-grid" style="display:none;">
							<!-- Photo -->
							<div class="review-panel panel-photo">
								<div class="panel-header">
									<h3><?php esc_html_e( 'Photo d’identité ANTS', 'certif-ephoto-control' ); ?></h3>
									<span id="modal-photo-score" class="score-badge">--</span>
								</div>

								<div class="comparison-images">
									<div class="img-box">
										<span class="img-caption"><?php esc_html_e( 'Originale', 'certif-ephoto-control' ); ?></span>
										<img id="modal-photo-orig" alt="<?php esc_attr_e( 'Photo originale', 'certif-ephoto-control' ); ?>">
										<span class="img-missing"><?php esc_html_e( 'Image indisponible', 'certif-ephoto-control' ); ?></span>
									</div>
									<div class="img-box">
										<span class="img-caption"><?php esc_html_e( 'Sortie ANTS (35×45mm)', 'certif-ephoto-control' ); ?></span>
										<img id="modal-photo-clean" alt="<?php esc_attr_e( 'Photo préparée', 'certif-ephoto-control' ); ?>">
										<span class="img-missing"><?php esc_html_e( 'Image indisponible', 'certif-ephoto-control' ); ?></span>
									</div>
								</div>

								<div class="checklist-box">
									<h4><?php esc_html_e( 'Critères de conformité ANTS', 'certif-ephoto-control' ); ?></h4>
									<div id="modal-photo-checks" class="checks-list"></div>
								</div>

								<div class="recrop-controls">
									<details>
										<summary><?php esc_html_e( 'Ajuster le cadrage (zoom & position)', 'certif-ephoto-control' ); ?></summary>
										<div class="recrop-sliders">
											<label for="slider-zoom">
												<?php esc_html_e( 'Zoom :', 'certif-ephoto-control' ); ?> <span id="val-zoom">1.00</span>×
												<small class="recrop-hint"><?php esc_html_e( '> 1 : cadre plus large, visage plus petit ; < 1 : visage plus grand.', 'certif-ephoto-control' ); ?></small>
												<input type="range" id="slider-zoom" min="0.5" max="2" step="0.05" value="1">
											</label>
											<label for="slider-dx">
												<?php esc_html_e( 'Décalage horizontal :', 'certif-ephoto-control' ); ?> <span id="val-dx">0</span> % <?php esc_html_e( 'du cadre', 'certif-ephoto-control' ); ?>
												<small class="recrop-hint"><?php esc_html_e( 'Négatif : vers la gauche ; positif : vers la droite.', 'certif-ephoto-control' ); ?></small>
												<input type="range" id="slider-dx" min="-0.5" max="0.5" step="0.01" value="0">
											</label>
											<label for="slider-dy">
												<?php esc_html_e( 'Décalage vertical :', 'certif-ephoto-control' ); ?> <span id="val-dy">0</span> % <?php esc_html_e( 'du cadre', 'certif-ephoto-control' ); ?>
												<small class="recrop-hint"><?php esc_html_e( 'Négatif : vers le haut ; positif : vers le bas.', 'certif-ephoto-control' ); ?></small>
												<input type="range" id="slider-dy" min="-0.5" max="0.5" step="0.01" value="0">
											</label>
											<div class="recrop-buttons">
												<button type="button" id="btn-reset-recrop" class="button button-small"><?php esc_html_e( 'Réinitialiser', 'certif-ephoto-control' ); ?></button>
												<button type="button" id="btn-apply-recrop" class="button button-small button-primary"><?php esc_html_e( 'Appliquer le recadrage', 'certif-ephoto-control' ); ?></button>
											</div>
										</div>
									</details>
								</div>
							</div>

							<!-- Signature -->
							<div class="review-panel panel-signature">
								<div class="panel-header">
									<h3><?php esc_html_e( 'Signature', 'certif-ephoto-control' ); ?></h3>
									<span id="modal-sign-score" class="score-badge">--</span>
								</div>

								<div class="comparison-images">
									<div class="img-box">
										<span class="img-caption"><?php esc_html_e( 'Originale', 'certif-ephoto-control' ); ?></span>
										<img id="modal-sign-orig" alt="<?php esc_attr_e( 'Signature originale', 'certif-ephoto-control' ); ?>">
										<span class="img-missing"><?php esc_html_e( 'Image indisponible', 'certif-ephoto-control' ); ?></span>
									</div>
									<div class="img-box">
										<span class="img-caption"><?php esc_html_e( 'Nettoyée (521×134px)', 'certif-ephoto-control' ); ?></span>
										<div class="signature-canvas-wrap">
											<img id="modal-sign-clean" alt="<?php esc_attr_e( 'Signature nettoyée', 'certif-ephoto-control' ); ?>">
											<span class="img-missing"><?php esc_html_e( 'Image indisponible', 'certif-ephoto-control' ); ?></span>
										</div>
									</div>
								</div>

								<div class="signature-actions">
									<h4><?php esc_html_e( 'Corriger l’orientation (par rapport à l’originale, sens horaire) :', 'certif-ephoto-control' ); ?></h4>
									<div class="rotate-btn-group">
										<button type="button" class="button btn-rotate" data-angle="0">0°</button>
										<button type="button" class="button btn-rotate" data-angle="90">90° ↻</button>
										<button type="button" class="button btn-rotate" data-angle="180">180°</button>
										<button type="button" class="button btn-rotate" data-angle="-90">90° ↺</button>
									</div>
									<div class="rotate-free">
										<label for="rotate-free-angle"><?php esc_html_e( 'Angle libre (°) :', 'certif-ephoto-control' ); ?></label>
										<input type="number" id="rotate-free-angle" min="-360" max="360" step="1" value="0" class="small-text">
										<button type="button" id="btn-rotate-free" class="button button-small"><?php esc_html_e( 'Appliquer', 'certif-ephoto-control' ); ?></button>
									</div>
								</div>

								<div class="checklist-box" style="margin-top:20px;">
									<h4><?php esc_html_e( 'Contrôle signature', 'certif-ephoto-control' ); ?></h4>
									<div id="modal-sign-checks" class="checks-list"></div>
								</div>
							</div>
						</div>
					</div>

					<footer class="certif-modal-footer">
						<div class="footer-left">
							<span id="modal-forward-status" class="forward-status-text"></span>
						</div>
						<div class="footer-actions">
							<button type="button" id="btn-reject-dossier" class="button button-link-delete" disabled>
								✘ <?php esc_html_e( 'Refuser le dossier', 'certif-ephoto-control' ); ?>
							</button>
							<button type="button" id="btn-accept-dossier" class="button button-primary button-large" disabled>
								✔ <?php esc_html_e( 'Accepter & transmettre', 'certif-ephoto-control' ); ?>
							</button>
						</div>
					</footer>
				</div>
			</div>
		</div>
			<?php
		} catch ( Throwable $e ) {
			echo '<div class="wrap"><div class="notice notice-error"><p><strong>'
				. esc_html__( 'Erreur dans le tableau de contrôle :', 'certif-ephoto-control' )
				. '</strong> '
				. esc_html( $e->getMessage() )
				. '</p></div></div>';
		}
	}

	/* ------------------------------------------------------------------
	 * AJAX helpers
	 * ------------------------------------------------------------------ */

	/**
	 * Nonce + capability check for every JSON handler.
	 */
	private static function ajax_guard() {
		if ( ! check_ajax_referer( self::AJAX_NONCE, 'nonce', false ) ) {
			wp_send_json_error( array( 'message' => __( 'Session expirée : rechargez la page.', 'certif-ephoto-control' ) ), 403 );
		}
		if ( ! current_user_can( self::capability() ) ) {
			wp_send_json_error( array( 'message' => __( 'Accès refusé.', 'certif-ephoto-control' ) ), 403 );
		}
		if ( ! function_exists( 'wc_get_order' ) ) {
			wp_send_json_error( array( 'message' => __( 'WooCommerce est requis.', 'certif-ephoto-control' ) ), 500 );
		}
	}

	/**
	 * Scalar POST value, unslashed and trimmed ('' when absent or not scalar).
	 */
	private static function post_scalar( $name ) {
		if ( ! isset( $_POST[ $name ] ) ) { // phpcs:ignore WordPress.Security.NonceVerification.Missing
			return '';
		}
		$value = wp_unslash( $_POST[ $name ] ); // phpcs:ignore WordPress.Security.NonceVerification.Missing,WordPress.Security.ValidatedSanitizedInput.InputNotSanitized
		return is_scalar( $value ) ? trim( (string) $value ) : '';
	}

	/**
	 * Load the order designated by POST order_id (sends a JSON error otherwise).
	 *
	 * @return WC_Order
	 */
	private static function order_from_request() {
		$raw      = self::post_scalar( 'order_id' );
		$order_id = preg_match( '/^\d{1,20}$/', $raw ) ? absint( $raw ) : 0;
		if ( ! $order_id ) {
			wp_send_json_error( array( 'message' => __( 'Commande invalide.', 'certif-ephoto-control' ) ), 400 );
		}
		$order = wc_get_order( $order_id );
		if ( ! $order instanceof WC_Order ) {
			wp_send_json_error( array( 'message' => __( 'Commande introuvable.', 'certif-ephoto-control' ) ), 404 );
		}
		return $order;
	}

	/**
	 * Submission id stored on the order (the only trusted source).
	 */
	private static function submission_id_of( $order ) {
		return Certif_Ephoto_Client::clean_submission_id( $order->get_meta( '_certif_ephoto_submission_id' ) );
	}

	/**
	 * Submission id of the order, or a JSON error when there is none.
	 */
	private static function require_submission_id( $order ) {
		$sub_id = self::submission_id_of( $order );
		if ( '' === $sub_id ) {
			wp_send_json_error(
				array(
					'message'  => __( 'Aucun dossier n’a encore été envoyé au service pour cette commande. Utilisez « Lancer l’analyse ».', 'certif-ephoto-control' ),
					'order_id' => $order->get_id(),
					'status'   => 'to_send',
				)
			);
		}
		return $sub_id;
	}

	/**
	 * Read the submission from the service and store its state on the order.
	 *
	 * @return array|WP_Error
	 */
	private static function refresh_submission( $order, $sub_id ) {
		$submission = Certif_Ephoto_Client::get_submission( $sub_id );
		if ( is_wp_error( $submission ) ) {
			if ( 404 === Certif_Ephoto_Client::error_status( $submission ) ) {
				Certif_Ephoto_Client::mark_missing( $order );
			}
			return $submission;
		}
		Certif_Ephoto_Client::store_submission_state( $order, $submission );
		return $submission;
	}

	/**
	 * Keep only what the review screen needs from a report.
	 */
	private static function report_subset( $report ) {
		if ( ! is_array( $report ) ) {
			return null;
		}
		$checks = array();
		if ( isset( $report['checks'] ) && is_array( $report['checks'] ) ) {
			foreach ( $report['checks'] as $check ) {
				if ( ! is_array( $check ) ) {
					continue;
				}
				$checks[] = array(
					'key'    => isset( $check['key'] ) && is_scalar( $check['key'] ) ? (string) $check['key'] : '',
					'label'  => isset( $check['label'] ) && is_scalar( $check['label'] ) ? (string) $check['label'] : '',
					'status' => isset( $check['status'] ) && in_array( $check['status'], array( 'pass', 'fail', 'unknown' ), true ) ? $check['status'] : 'unknown',
					'detail' => isset( $check['detail'] ) && is_scalar( $check['detail'] ) ? (string) $check['detail'] : '',
				);
			}
		}
		return array(
			'score'     => isset( $report['score'] ) && is_numeric( $report['score'] ) ? (float) $report['score'] : null,
			'compliant' => isset( $report['compliant'] ) ? (bool) $report['compliant'] : null,
			'error'     => isset( $report['error'] ) && is_scalar( $report['error'] ) ? (string) $report['error'] : '',
			'width'     => isset( $report['width'] ) && is_numeric( $report['width'] ) ? (int) $report['width'] : null,
			'height'    => isset( $report['height'] ) && is_numeric( $report['height'] ) ? (int) $report['height'] : null,
			'checks'    => $checks,
		);
	}

	/**
	 * JSON payload consumed by admin.js renderReview().
	 */
	private static function build_review_payload( $order, $submission ) {
		$sub   = is_array( $submission ) ? $submission : array();
		$files = array();
		foreach ( Certif_Ephoto_Client::FILE_KINDS as $kind ) {
			$files[ $kind ] = isset( $sub['files'] ) && is_array( $sub['files'] ) && ! empty( $sub['files'][ $kind ] );
		}

		$status = isset( $sub['status'] ) && is_string( $sub['status'] ) && in_array( $sub['status'], Certif_Ephoto_Client::SERVICE_STATUSES, true )
			? $sub['status']
			: (string) $order->get_meta( '_certif_ephoto_status' );

		$text = function ( $key ) use ( $sub ) {
			return isset( $sub[ $key ] ) && is_scalar( $sub[ $key ] ) ? (string) $sub[ $key ] : '';
		};
		$score = function ( $key ) use ( $sub ) {
			return isset( $sub[ $key ] ) && is_numeric( $sub[ $key ] ) ? (float) $sub[ $key ] : null;
		};

		return array(
			'order_id'     => $order->get_id(),
			'order_number' => (string) $order->get_order_number(),
			'customer'     => array(
				'name'  => trim( $order->get_billing_first_name() . ' ' . $order->get_billing_last_name() ),
				'email' => (string) $order->get_billing_email(),
			),
			'submission'   => array(
				'submission_id'   => self::submission_id_of( $order ),
				'status'          => $status,
				'photo'           => self::report_subset( isset( $sub['photo'] ) ? $sub['photo'] : null ),
				'signature'       => self::report_subset( isset( $sub['signature'] ) ? $sub['signature'] : null ),
				'photo_score'     => $score( 'photo_score' ),
				'signature_score' => $score( 'signature_score' ),
				'forward_status'  => $text( 'forward_status' ),
				'reviewer_note'   => $text( 'reviewer_note' ),
				'error'           => $text( 'error' ),
				'decided_at'      => $text( 'decided_at' ),
				'files'           => $files,
				'accept_ready'    => isset( $sub['accept_ready'] ) && true === $sub['accept_ready'],
			),
		);
	}

	/**
	 * Re-read the submission and send it as a success response.
	 */
	private static function send_review( $order, $sub_id, $extra = array() ) {
		$submission = self::refresh_submission( $order, $sub_id );
		if ( is_wp_error( $submission ) ) {
			wp_send_json_error(
				array(
					'message'  => $submission->get_error_message(),
					'order_id' => $order->get_id(),
					'status'   => (string) $order->get_meta( '_certif_ephoto_status' ),
				)
			);
		}
		wp_send_json_success( array_merge( $extra, array( 'review' => self::build_review_payload( $order, $submission ) ) ) );
	}

	/**
	 * Send a service error; on 409 the fresh state is attached so the UI can resync.
	 */
	private static function send_service_error( $order, $sub_id, $error ) {
		$data   = array(
			'message'  => $error->get_error_message(),
			'order_id' => $order->get_id(),
		);
		$status = Certif_Ephoto_Client::error_status( $error );
		if ( 409 === $status || 404 === $status ) {
			$fresh = self::refresh_submission( $order, $sub_id );
			if ( ! is_wp_error( $fresh ) ) {
				$data['review'] = self::build_review_payload( $order, $fresh );
			}
		}
		$data['status'] = (string) $order->get_meta( '_certif_ephoto_status' );
		wp_send_json_error( $data );
	}

	/**
	 * Parse a float POST field ("," accepted). Null when invalid.
	 */
	private static function post_float( $name, $default ) {
		$raw = str_replace( ',', '.', self::post_scalar( $name ) );
		if ( '' === $raw ) {
			return (float) $default;
		}
		if ( ! is_numeric( $raw ) ) {
			return null;
		}
		$value = (float) $raw;
		if ( is_nan( $value ) || is_infinite( $value ) ) {
			return null;
		}
		return $value;
	}

	/* ------------------------------------------------------------------
	 * AJAX handlers
	 * ------------------------------------------------------------------ */

	/**
	 * AJAX: get the review data of an order (view only, never ingests).
	 */
	public static function ajax_get_order_review() {
		self::ajax_guard();
		$order  = self::order_from_request();
		$sub_id = self::require_submission_id( $order );
		self::send_review( $order, $sub_id );
	}

	/**
	 * AJAX: manual recrop. zoom 0.5–2.0, dx/dy -0.5–0.5 (fractions of the crop).
	 */
	public static function ajax_recrop_photo() {
		self::ajax_guard();
		$order  = self::order_from_request();
		$sub_id = self::require_submission_id( $order );

		$zoom = self::post_float( 'zoom', 1.0 );
		$dx   = self::post_float( 'dx', 0.0 );
		$dy   = self::post_float( 'dy', 0.0 );
		if ( null === $zoom || null === $dx || null === $dy ) {
			wp_send_json_error( array( 'message' => __( 'Valeurs de recadrage invalides.', 'certif-ephoto-control' ) ), 400 );
		}
		$zoom = max( 0.5, min( 2.0, $zoom ) );
		$dx   = max( -0.5, min( 0.5, $dx ) );
		$dy   = max( -0.5, min( 0.5, $dy ) );

		$result = Certif_Ephoto_Client::recrop( $sub_id, $zoom, $dx, $dy );
		if ( is_wp_error( $result ) ) {
			self::send_service_error( $order, $sub_id, $result );
		}
		self::send_review( $order, $sub_id );
	}

	/**
	 * AJAX: rotate the signature (clockwise degrees, negative allowed).
	 */
	public static function ajax_rotate_signature() {
		self::ajax_guard();
		$order  = self::order_from_request();
		$sub_id = self::require_submission_id( $order );

		$raw = self::post_scalar( 'rotation' );
		if ( ! preg_match( '/^-?\d{1,4}$/', $raw ) ) {
			wp_send_json_error( array( 'message' => __( 'Angle de rotation invalide.', 'certif-ephoto-control' ) ), 400 );
		}
		$rotation = intval( $raw );
		if ( $rotation < -360 || $rotation > 360 ) {
			wp_send_json_error( array( 'message' => __( 'Angle de rotation invalide (entre -360 et 360).', 'certif-ephoto-control' ) ), 400 );
		}

		$result = Certif_Ephoto_Client::rotate_signature( $sub_id, $rotation );
		if ( is_wp_error( $result ) ) {
			self::send_service_error( $order, $sub_id, $result );
		}
		self::send_review( $order, $sub_id );
	}

	/**
	 * AJAX: accept or reject.
	 */
	public static function ajax_validate_order() {
		self::ajax_guard();
		$order  = self::order_from_request();
		$sub_id = self::require_submission_id( $order );

		$action = sanitize_key( self::post_scalar( 'action_type' ) );
		if ( ! in_array( $action, array( 'accept', 'reject' ), true ) ) {
			wp_send_json_error( array( 'message' => __( 'Paramètres invalides.', 'certif-ephoto-control' ) ), 400 );
		}

		$reason = sanitize_textarea_field( self::post_scalar( 'reason' ) );
		if ( function_exists( 'mb_substr' ) ) {
			$reason = mb_substr( $reason, 0, 1000, 'UTF-8' );
		}
		if ( 'reject' === $action && '' === trim( $reason ) ) {
			wp_send_json_error( array( 'message' => __( 'Le motif du refus est obligatoire.', 'certif-ephoto-control' ) ), 400 );
		}

		// Always decide on the current service state, never on the browser's.
		$current = self::refresh_submission( $order, $sub_id );
		if ( is_wp_error( $current ) ) {
			wp_send_json_error(
				array(
					'message'  => $current->get_error_message(),
					'order_id' => $order->get_id(),
					'status'   => (string) $order->get_meta( '_certif_ephoto_status' ),
				)
			);
		}

		$state = isset( $current['status'] ) && is_string( $current['status'] ) ? $current['status'] : '';
		if ( 'accepted' === $state || 'rejected' === $state ) {
			wp_send_json_error(
				array(
					'message'  => sprintf( __( 'Ce dossier a déjà été décidé (%s). La commande a été mise à jour en conséquence.', 'certif-ephoto-control' ), self::status_label( $state ) ),
					'order_id' => $order->get_id(),
					'status'   => $state,
					'review'   => self::build_review_payload( $order, $current ),
				)
			);
		}
		if ( 'processing' === $state ) {
			wp_send_json_error(
				array(
					'message'  => __( 'Le dossier est encore en cours de traitement par le service.', 'certif-ephoto-control' ),
					'order_id' => $order->get_id(),
					'status'   => $state,
					'review'   => self::build_review_payload( $order, $current ),
				)
			);
		}
		if ( 'accept' === $action && ! ( isset( $current['accept_ready'] ) && true === $current['accept_ready'] ) ) {
			wp_send_json_error(
				array(
					'message'  => __( 'Ce dossier n’est pas prêt à être accepté (fichier manquant ou analyse incomplète).', 'certif-ephoto-control' ),
					'order_id' => $order->get_id(),
					'status'   => $state,
					'review'   => self::build_review_payload( $order, $current ),
				)
			);
		}

		$result = Certif_Ephoto_Client::validate( $sub_id, $action, $reason );
		if ( is_wp_error( $result ) ) {
			// 409 (already decided / transmission in progress): resync before reporting.
			self::send_service_error( $order, $sub_id, $result );
		}

		$new_status = ( isset( $result['status'] ) && in_array( $result['status'], array( 'accepted', 'rejected' ), true ) )
			? $result['status']
			: ( 'accept' === $action ? 'accepted' : 'rejected' );
		$decided_at = isset( $result['decided_at'] ) && is_scalar( $result['decided_at'] ) ? sanitize_text_field( (string) $result['decided_at'] ) : '';

		Certif_Ephoto_Client::apply_decision( $order, $new_status, $reason, Certif_Ephoto_Client::reviewer_label(), $decided_at, false );
		if ( isset( $result['forward_status'] ) && is_scalar( $result['forward_status'] ) ) {
			$order->update_meta_data( '_certif_ephoto_forward_status', sanitize_text_field( (string) $result['forward_status'] ) );
			$order->save();
		}

		$extra = array(
			'order_id'       => $order->get_id(),
			'status'         => $new_status,
			'forward_status' => isset( $result['forward_status'] ) && is_scalar( $result['forward_status'] ) ? (string) $result['forward_status'] : '',
		);

		$fresh = self::refresh_submission( $order, $sub_id );
		if ( is_wp_error( $fresh ) ) {
			wp_send_json_success( $extra );
		}
		$extra['review'] = self::build_review_payload( $order, $fresh );
		wp_send_json_success( $extra );
	}

	/**
	 * AJAX: explicit ingest ("Lancer l'analyse" / "Relancer l'analyse").
	 * Live dossiers (processing/pending/accepted) are never re-sent.
	 */
	public static function ajax_sync_order() {
		self::ajax_guard();
		$order = self::order_from_request();

		$result = Certif_Ephoto_Client::ingest_order( $order->get_id(), true );
		if ( is_wp_error( $result ) ) {
			wp_send_json_error(
				array(
					'message'  => $result->get_error_message(),
					'order_id' => $order->get_id(),
				)
			);
		}

		wp_send_json_success(
			array(
				'order_id'  => $order->get_id(),
				'status'    => isset( $result['status'] ) ? (string) $result['status'] : 'processing',
				'existing'  => ! empty( $result['existing'] ),
				'duplicate' => ! empty( $result['duplicate'] ),
			)
		);
	}

	/**
	 * AJAX (GET): authenticated image proxy. The service key never leaves the server.
	 */
	public static function ajax_image() {
		if ( ! check_ajax_referer( self::IMAGE_NONCE, '_wpnonce', false ) ) {
			self::image_error( 403 );
		}
		if ( ! current_user_can( self::capability() ) ) {
			self::image_error( 403 );
		}
		if ( ! function_exists( 'wc_get_order' ) ) {
			self::image_error( 500 );
		}

		$raw_id   = isset( $_GET['order_id'] ) && is_scalar( $_GET['order_id'] ) ? (string) wp_unslash( $_GET['order_id'] ) : '';
		$kind     = isset( $_GET['kind'] ) && is_scalar( $_GET['kind'] ) ? (string) wp_unslash( $_GET['kind'] ) : '';
		$order_id = preg_match( '/^\d{1,20}$/', $raw_id ) ? absint( $raw_id ) : 0;
		if ( ! $order_id || ! in_array( $kind, Certif_Ephoto_Client::FILE_KINDS, true ) ) {
			self::image_error( 400 );
		}

		$order = wc_get_order( $order_id );
		if ( ! $order instanceof WC_Order ) {
			self::image_error( 404 );
		}
		$sub_id = self::submission_id_of( $order );
		if ( '' === $sub_id ) {
			self::image_error( 404 );
		}

		$file = Certif_Ephoto_Client::fetch_file( $sub_id, $kind );
		if ( is_wp_error( $file ) ) {
			if ( defined( 'WP_DEBUG' ) && WP_DEBUG ) {
				error_log( sprintf( '[Certif ID] ajax_image: échec de récupération du fichier %s pour la commande #%d : %s', $kind, $order_id, $file->get_error_message() ) );
			}
			self::image_error( 404 === Certif_Ephoto_Client::error_status( $file ) ? 404 : 502 );
		}

		$type = strtolower( trim( (string) strtok( (string) $file['content_type'], ';' ) ) );
		if ( ! in_array( $type, array( 'image/jpeg', 'image/png', 'image/webp', 'image/bmp', 'image/x-ms-bmp', 'image/tiff' ), true ) || '' === $file['body'] ) {
			self::image_error( 502 );
		}

		self::clean_output_buffers();
		status_header( 200 );
		header( 'Content-Type: ' . $type );
		header( 'Cache-Control: no-store, private' );
		header( 'Pragma: no-cache' );
		header( 'X-Content-Type-Options: nosniff' );
		header( 'Content-Disposition: inline' );
		echo $file['body']; // phpcs:ignore WordPress.Security.EscapeOutput.OutputNotEscaped -- binary image bytes.
		exit;
	}

	/**
	 * Minimal error response for the image proxy.
	 */
	private static function image_error( $status ) {
		self::clean_output_buffers();
		status_header( (int) $status );
		header( 'Content-Type: text/plain; charset=utf-8' );
		header( 'Cache-Control: no-store, private' );
		header( 'X-Content-Type-Options: nosniff' );
		echo esc_html( (string) (int) $status );
		exit;
	}

	/**
	 * Drop any stray output before sending binary data.
	 */
	private static function clean_output_buffers() {
		$levels = ob_get_level();
		for ( $i = 0; $i < $levels; $i++ ) {
			if ( ! @ob_end_clean() ) { // phpcs:ignore WordPress.PHP.NoSilencedErrors.Discouraged
				break;
			}
		}
	}
}

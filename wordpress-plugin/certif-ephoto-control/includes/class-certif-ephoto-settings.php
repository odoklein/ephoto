<?php
/**
 * Settings and diagnostics page for Certif ID Ephoto.
 *
 * Secrets are never printed back; wp-config.php constants are never copied to
 * the database; options are stored with autoload disabled.
 */

if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

class Certif_Ephoto_Settings {

	const PAGE        = 'certif-ephoto-control-settings';
	const SAVE_ACTION = 'certif_ephoto_save_settings';

	public static function init() {
		add_action( 'admin_post_' . self::SAVE_ACTION, array( __CLASS__, 'handle_save' ) );
		add_action( 'admin_init', array( __CLASS__, 'maybe_upgrade' ) );
	}

	/**
	 * Options owned by the plugin.
	 */
	public static function option_names() {
		return array(
			'certif_ephoto_service_url',
			'certif_ephoto_api_key',
			'certif_ephoto_review_key',
			'certif_ephoto_auto_ingest',
			'certif_ephoto_accepted_status',
			'certif_ephoto_rejected_status',
			'certif_ephoto_photo_meta_key',
			'certif_ephoto_sign_meta_key',
		);
	}

	/**
	 * wp-config.php constant overriding an option ('' if none).
	 */
	public static function constant_for( $option ) {
		$map = array(
			'certif_ephoto_service_url' => 'CERTIF_EPHOTO_SERVICE_URL',
			'certif_ephoto_api_key'     => 'CERTIF_EPHOTO_API_KEY',
			'certif_ephoto_review_key'  => 'CERTIF_EPHOTO_REVIEW_API_KEY',
		);
		return isset( $map[ $option ] ) ? $map[ $option ] : '';
	}

	public static function is_constant_defined( $option ) {
		$constant = self::constant_for( $option );
		return '' !== $constant && defined( $constant );
	}

	/**
	 * One-time migration when the plugin version changes.
	 */
	public static function maybe_upgrade() {
		if ( CERTIF_EPHOTO_VERSION === get_option( 'certif_ephoto_db_version', '' ) ) {
			return;
		}
		// The Make webhook is configured on the service (MAKE_WEBHOOK_URL), not here.
		delete_option( 'certif_ephoto_make_webhook_url' );
		// Never keep a DB copy of a value defined in wp-config.php.
		foreach ( array( 'certif_ephoto_service_url', 'certif_ephoto_api_key', 'certif_ephoto_review_key' ) as $option ) {
			if ( self::is_constant_defined( $option ) ) {
				delete_option( $option );
			}
		}
		self::disable_autoload();
		update_option( 'certif_ephoto_db_version', CERTIF_EPHOTO_VERSION, false );
	}

	/**
	 * Turn autoload off for options created by older versions (WP 6.4+).
	 */
	private static function disable_autoload() {
		if ( function_exists( 'wp_set_options_autoload' ) ) {
			wp_set_options_autoload( self::option_names(), false );
		}
	}

	/**
	 * Unslashed, trimmed string from $_POST ('' when absent or not a string).
	 */
	private static function post_string( $name ) {
		if ( ! isset( $_POST[ $name ] ) || ! is_string( $_POST[ $name ] ) ) { // phpcs:ignore WordPress.Security.NonceVerification.Missing
			return '';
		}
		return trim( wp_unslash( $_POST[ $name ] ) ); // phpcs:ignore WordPress.Security.NonceVerification.Missing,WordPress.Security.ValidatedSanitizedInput.InputNotSanitized
	}

	/**
	 * Secrets: no sanitize_text_field() (it mangles "%xx"); only trim + strip control chars.
	 */
	private static function sanitize_secret( $value ) {
		$value = preg_replace( '/[\x00-\x1F\x7F]/', '', (string) $value );
		return is_string( $value ) ? trim( $value ) : '';
	}

	/**
	 * Order status slugs without the "wc-" prefix.
	 */
	private static function order_status_slugs() {
		if ( ! function_exists( 'wc_get_order_statuses' ) ) {
			return array();
		}
		$slugs = array();
		foreach ( array_keys( wc_get_order_statuses() ) as $key ) {
			$slugs[] = preg_replace( '/^wc-/', '', $key );
		}
		return $slugs;
	}

	/**
	 * admin-post handler for the settings form.
	 */
	public static function handle_save() {
		if ( ! current_user_can( 'manage_options' ) ) {
			wp_die( esc_html__( 'Accès refusé.', 'certif-ephoto-control' ), '', array( 'response' => 403 ) );
		}
		check_admin_referer( self::SAVE_ACTION );

		$notices = array();

		// Service URL (https only, http for localhost).
		if ( self::is_constant_defined( 'certif_ephoto_service_url' ) ) {
			delete_option( 'certif_ephoto_service_url' );
		} else {
			$raw = self::post_string( 'certif_ephoto_service_url' );
			if ( '' === $raw ) {
				update_option( 'certif_ephoto_service_url', '', false );
			} else {
				$url = untrailingslashit( esc_url_raw( $raw, array( 'http', 'https' ) ) );
				if ( '' !== $url && Certif_Ephoto_Client::is_allowed_service_url( $url ) ) {
					update_option( 'certif_ephoto_service_url', $url, false );
				} else {
					$notices[] = 'insecure_url';
				}
			}
		}

		// Secrets: an empty field keeps the stored value.
		foreach ( array( 'certif_ephoto_api_key', 'certif_ephoto_review_key' ) as $secret ) {
			if ( self::is_constant_defined( $secret ) ) {
				delete_option( $secret );
				continue;
			}
			if ( '' !== self::post_string( $secret . '_clear' ) ) {
				delete_option( $secret );
				continue;
			}
			$value = isset( $_POST[ $secret ] ) && is_string( $_POST[ $secret ] ) // phpcs:ignore WordPress.Security.NonceVerification.Missing
				? self::sanitize_secret( wp_unslash( $_POST[ $secret ] ) ) // phpcs:ignore WordPress.Security.NonceVerification.Missing,WordPress.Security.ValidatedSanitizedInput.InputNotSanitized
				: '';
			if ( '' !== $value ) {
				update_option( $secret, $value, false );
			}
		}

		update_option( 'certif_ephoto_auto_ingest', '' !== self::post_string( 'certif_ephoto_auto_ingest' ) ? 'yes' : 'no', false );

		$valid_statuses = self::order_status_slugs();
		$status_options = array(
			'certif_ephoto_accepted_status' => 'completed',
			'certif_ephoto_rejected_status' => 'failed',
		);
		foreach ( $status_options as $option => $default ) {
			$value = sanitize_key( self::post_string( $option ) );
			if ( 'none' !== $value && ! empty( $valid_statuses ) && ! in_array( $value, $valid_statuses, true ) ) {
				$value = $default;
			}
			if ( '' === $value ) {
				$value = $default;
			}
			update_option( $option, $value, false );
		}

		update_option( 'certif_ephoto_photo_meta_key', sanitize_text_field( self::post_string( 'certif_ephoto_photo_meta_key' ) ), false );
		update_option( 'certif_ephoto_sign_meta_key', sanitize_text_field( self::post_string( 'certif_ephoto_sign_meta_key' ) ), false );

		self::disable_autoload();

		if ( empty( $notices ) ) {
			$notices[] = 'saved';
		}
		wp_safe_redirect(
			add_query_arg(
				array(
					'page'          => self::PAGE,
					'certif_notice' => implode( ',', $notices ),
				),
				admin_url( 'admin.php' )
			)
		);
		exit;
	}

	/**
	 * Render a secret field (never prints the stored value).
	 */
	private static function render_secret_field( $option, $label, $description ) {
		$constant = self::is_constant_defined( $option );
		$stored   = '' !== trim( (string) get_option( $option, '' ) );
		?>
		<tr>
			<th scope="row"><label for="<?php echo esc_attr( $option ); ?>"><?php echo esc_html( $label ); ?></label></th>
			<td>
				<?php if ( $constant ) : ?>
					<input type="password" id="<?php echo esc_attr( $option ); ?>" class="regular-text" value="" disabled
						placeholder="<?php esc_attr_e( 'défini dans wp-config.php', 'certif-ephoto-control' ); ?>">
					<p class="description">
						<?php
						echo esc_html(
							sprintf(
								/* translators: %s: constant name */
								__( 'Défini dans wp-config.php (%s) : non modifiable ici et jamais enregistré en base.', 'certif-ephoto-control' ),
								self::constant_for( $option )
							)
						);
						?>
					</p>
				<?php else : ?>
					<input type="password" id="<?php echo esc_attr( $option ); ?>" name="<?php echo esc_attr( $option ); ?>" class="regular-text" value=""
						autocomplete="new-password" spellcheck="false"
						placeholder="<?php echo $stored ? esc_attr__( '•••• (défini)', 'certif-ephoto-control' ) : ''; ?>">
					<?php if ( $stored ) : ?>
						<label style="margin-left:8px;">
							<input type="checkbox" name="<?php echo esc_attr( $option . '_clear' ); ?>" value="1">
							<?php esc_html_e( 'Effacer la clé enregistrée', 'certif-ephoto-control' ); ?>
						</label>
					<?php endif; ?>
					<p class="description"><?php echo esc_html( $description ); ?>
						<?php esc_html_e( 'Laissez vide pour conserver la valeur actuelle.', 'certif-ephoto-control' ); ?></p>
				<?php endif; ?>
			</td>
		</tr>
		<?php
	}

	public static function render() {
		if ( ! current_user_can( 'manage_options' ) ) {
			wp_die( esc_html__( 'Accès refusé.', 'certif-ephoto-control' ), '', array( 'response' => 403 ) );
		}

		$configured_url  = Certif_Ephoto_Client::get_configured_service_url();
		$url_is_constant = self::is_constant_defined( 'certif_ephoto_service_url' );
		$url_rejected    = '' !== $configured_url && ! Certif_Ephoto_Client::is_allowed_service_url( $configured_url );
		$auto_ingest     = get_option( 'certif_ephoto_auto_ingest', 'yes' );
		$accepted_status = get_option( 'certif_ephoto_accepted_status', 'completed' );
		$rejected_status = get_option( 'certif_ephoto_rejected_status', 'failed' );
		$photo_key       = get_option( 'certif_ephoto_photo_meta_key', '' );
		$sign_key        = get_option( 'certif_ephoto_sign_meta_key', '' );
		$statuses        = function_exists( 'wc_get_order_statuses' ) ? wc_get_order_statuses() : array();

		// Notices after a save.
		$notices = array();
		if ( isset( $_GET['certif_notice'] ) && is_string( $_GET['certif_notice'] ) ) { // phpcs:ignore WordPress.Security.NonceVerification.Recommended
			$notices = array_map( 'sanitize_key', explode( ',', wp_unslash( $_GET['certif_notice'] ) ) ); // phpcs:ignore WordPress.Security.NonceVerification.Recommended,WordPress.Security.ValidatedSanitizedInput.InputNotSanitized
		}

		// Explicit connection test (never run automatically on page load).
		$health = null;
		$keys   = null;
		if ( isset( $_POST['certif_test_connection'] ) ) { // phpcs:ignore WordPress.Security.NonceVerification.Missing
			check_admin_referer( 'certif_ephoto_test_connection', 'certif_test_nonce' );
			$health = Certif_Ephoto_Client::test_connection();
			if ( $health['ok'] ) {
				$keys = Certif_Ephoto_Client::test_keys();
			}
		}

		// Order detection diagnostic.
		$inspect_result = null;
		$test_order_id  = 0;
		if ( isset( $_POST['certif_inspect_order_id'] ) ) { // phpcs:ignore WordPress.Security.NonceVerification.Missing
			check_admin_referer( 'certif_inspect_order_action', 'certif_inspect_nonce' );
			$test_order_id = absint( wp_unslash( $_POST['certif_inspect_order_id'] ) );
			if ( $test_order_id > 0 ) {
				$inspect_result = Certif_Ephoto_Order_Reader::inspect_order( $test_order_id );
			}
		}
		?>
		<div class="wrap certif-settings-wrap">
			<h1><?php esc_html_e( 'Réglages – Contrôle photos ANTS & Signature', 'certif-ephoto-control' ); ?></h1>

			<?php if ( in_array( 'saved', $notices, true ) ) : ?>
				<div class="notice notice-success is-dismissible"><p><?php esc_html_e( 'Réglages enregistrés.', 'certif-ephoto-control' ); ?></p></div>
			<?php endif; ?>
			<?php if ( in_array( 'insecure_url', $notices, true ) ) : ?>
				<div class="notice notice-error"><p><?php esc_html_e( 'Adresse du service refusée : elle doit commencer par https:// (http:// accepté uniquement pour localhost / 127.0.0.1). Les autres réglages ont été enregistrés.', 'certif-ephoto-control' ); ?></p></div>
			<?php endif; ?>
			<?php if ( $url_rejected ) : ?>
				<div class="notice notice-error"><p><?php esc_html_e( 'L’adresse du service actuellement configurée n’est pas en https : aucune requête n’est envoyée tant qu’elle n’est pas corrigée.', 'certif-ephoto-control' ); ?></p></div>
			<?php endif; ?>

			<div class="notice notice-info" style="margin-top:15px;">
				<p>
					<strong><?php esc_html_e( 'Une seule voie d’entrée :', 'certif-ephoto-control' ); ?></strong>
					<?php esc_html_e( 'les dossiers doivent arriver au service soit par l’envoi automatique de ce plugin (case « Envoi automatique » ci-dessous), soit par le scénario Make A/A2 qui appelle /api/v1/ingest — jamais les deux à la fois. Si Make A/A2 est actif, décochez l’envoi automatique.', 'certif-ephoto-control' ); ?>
				</p>
				<p>
					<?php esc_html_e( 'La transmission vers Make / ePhoto après acceptation est faite par le service lui-même (variable MAKE_WEBHOOK_URL du service) : il n’y a rien à configurer ici pour la sortie.', 'certif-ephoto-control' ); ?>
				</p>
			</div>

			<div class="card" style="max-width:100%;margin-top:20px;padding:15px 20px;">
				<h2><?php esc_html_e( 'État du service de contrôle', 'certif-ephoto-control' ); ?></h2>
				<form method="post" style="margin:10px 0;">
					<?php wp_nonce_field( 'certif_ephoto_test_connection', 'certif_test_nonce' ); ?>
					<button type="submit" name="certif_test_connection" value="1" class="button button-secondary"><?php esc_html_e( 'Tester la connexion', 'certif-ephoto-control' ); ?></button>
				</form>
				<?php if ( null !== $health ) : ?>
					<?php if ( $health['ok'] ) : ?>
						<p style="color:<?php echo 'degraded' === $health['status'] ? '#b45309' : '#059669'; ?>;font-weight:600;font-size:14px;">
							<?php echo esc_html( $health['message'] ); ?>
						</p>
						<ul style="margin-left:20px;list-style:disc;color:#334155;">
							<li><strong><?php esc_html_e( 'Statut :', 'certif-ephoto-control' ); ?></strong> <code><?php echo esc_html( $health['status'] ); ?></code></li>
							<li><strong><?php esc_html_e( 'Détecteur de visage :', 'certif-ephoto-control' ); ?></strong> <code><?php echo esc_html( $health['detector'] ); ?></code></li>
							<?php if ( is_array( $keys ) ) : ?>
								<li><strong><?php esc_html_e( 'Clé d’ingestion :', 'certif-ephoto-control' ); ?></strong>
									<span style="color:<?php echo $keys['ingest']['ok'] ? '#059669' : '#dc2626'; ?>;"><?php echo esc_html( $keys['ingest']['message'] ); ?></span></li>
								<li><strong><?php esc_html_e( 'Clé de contrôle :', 'certif-ephoto-control' ); ?></strong>
									<span style="color:<?php echo $keys['review']['ok'] ? '#059669' : '#dc2626'; ?>;"><?php echo esc_html( $keys['review']['message'] ); ?></span></li>
							<?php endif; ?>
						</ul>
					<?php else : ?>
						<p style="color:#dc2626;font-weight:600;font-size:14px;">✘ <?php echo esc_html( $health['message'] ); ?></p>
						<p class="description"><?php esc_html_e( 'Vérifiez l’adresse et que le service tourne.', 'certif-ephoto-control' ); ?></p>
					<?php endif; ?>
				<?php endif; ?>
			</div>

			<form method="post" action="<?php echo esc_url( admin_url( 'admin-post.php' ) ); ?>" style="margin-top:20px;">
				<input type="hidden" name="action" value="<?php echo esc_attr( self::SAVE_ACTION ); ?>">
				<?php wp_nonce_field( self::SAVE_ACTION ); ?>

				<table class="form-table" role="presentation">
					<tr>
						<th scope="row"><label for="certif_ephoto_service_url"><?php esc_html_e( 'Adresse du service de contrôle', 'certif-ephoto-control' ); ?></label></th>
						<td>
							<?php if ( $url_is_constant ) : ?>
								<input type="url" id="certif_ephoto_service_url" class="regular-text" value="<?php echo esc_attr( $configured_url ); ?>" disabled>
								<p class="description"><?php esc_html_e( 'Défini dans wp-config.php (CERTIF_EPHOTO_SERVICE_URL).', 'certif-ephoto-control' ); ?></p>
							<?php else : ?>
								<input type="url" id="certif_ephoto_service_url" name="certif_ephoto_service_url" class="regular-text"
									value="<?php echo esc_attr( $configured_url ); ?>" placeholder="https://photos.exemple.fr">
								<p class="description"><?php esc_html_e( 'URL de base du service, en https (ex. https://photos.certif-idphoto.fr, sans slash final). http:// accepté uniquement pour localhost / 127.0.0.1.', 'certif-ephoto-control' ); ?></p>
							<?php endif; ?>
						</td>
					</tr>

					<?php
					self::render_secret_field(
						'certif_ephoto_api_key',
						__( 'Clé d’ingestion (INGEST_API_KEY)', 'certif-ephoto-control' ),
						__( 'Utilisée pour envoyer les dossiers au service (POST /api/v1/ingest).', 'certif-ephoto-control' )
					);
					self::render_secret_field(
						'certif_ephoto_review_key',
						__( 'Clé de contrôle (REVIEW_API_KEY)', 'certif-ephoto-control' ),
						__( 'Utilisée pour l’écran de contrôle : lecture des dossiers, images, recadrage, rotation, acceptation / refus. Si laissée vide, la clé d’ingestion est utilisée automatiquement (recommandé si vous utilisez une clé unique).', 'certif-ephoto-control' )
					);
					?>

					<tr>
						<th scope="row"><?php esc_html_e( 'Envoi automatique', 'certif-ephoto-control' ); ?></th>
						<td>
							<label>
								<input type="checkbox" name="certif_ephoto_auto_ingest" value="yes" <?php checked( 'yes', $auto_ingest ); ?>>
								<?php esc_html_e( 'Envoyer automatiquement les commandes payées au service (voie d’entrée « plugin »).', 'certif-ephoto-control' ); ?>
							</label>
							<p class="description"><?php esc_html_e( 'Décochez si les dossiers arrivent déjà par le scénario Make A/A2. L’envoi se fait en arrière-plan (Action Scheduler) ; en cas d’échec, une note est ajoutée à la commande.', 'certif-ephoto-control' ); ?></p>
						</td>
					</tr>

					<tr>
						<th scope="row"><label for="certif_ephoto_accepted_status"><?php esc_html_e( 'Statut commande après acceptation', 'certif-ephoto-control' ); ?></label></th>
						<td>
							<select id="certif_ephoto_accepted_status" name="certif_ephoto_accepted_status">
								<option value="none" <?php selected( 'none', $accepted_status ); ?>><?php esc_html_e( '— Ne pas modifier le statut —', 'certif-ephoto-control' ); ?></option>
								<?php
								foreach ( $statuses as $st_key => $st_label ) :
									$clean_key = preg_replace( '/^wc-/', '', $st_key );
									?>
									<option value="<?php echo esc_attr( $clean_key ); ?>" <?php selected( $clean_key, $accepted_status ); ?>>
										<?php echo esc_html( $st_label ); ?> (<?php echo esc_html( $clean_key ); ?>)
									</option>
								<?php endforeach; ?>
							</select>
							<p class="description"><?php esc_html_e( 'Statut appliqué à la commande quand le dossier est accepté (recommandé : Terminée / completed).', 'certif-ephoto-control' ); ?></p>
						</td>
					</tr>

					<tr>
						<th scope="row"><label for="certif_ephoto_rejected_status"><?php esc_html_e( 'Statut commande après refus', 'certif-ephoto-control' ); ?></label></th>
						<td>
							<select id="certif_ephoto_rejected_status" name="certif_ephoto_rejected_status">
								<option value="none" <?php selected( 'none', $rejected_status ); ?>><?php esc_html_e( '— Ne pas modifier le statut —', 'certif-ephoto-control' ); ?></option>
								<?php
								foreach ( $statuses as $st_key => $st_label ) :
									$clean_key = preg_replace( '/^wc-/', '', $st_key );
									?>
									<option value="<?php echo esc_attr( $clean_key ); ?>" <?php selected( $clean_key, $rejected_status ); ?>>
										<?php echo esc_html( $st_label ); ?> (<?php echo esc_html( $clean_key ); ?>)
									</option>
								<?php endforeach; ?>
							</select>
							<p class="description"><?php esc_html_e( 'Statut appliqué si le dossier est refusé (ex. Échouée / failed ou En attente / on-hold).', 'certif-ephoto-control' ); ?></p>
						</td>
					</tr>

					<tr>
						<th scope="row"><?php esc_html_e( 'Champs personnalisés (optionnel)', 'certif-ephoto-control' ); ?></th>
						<td>
							<p>
								<label for="certif_ephoto_photo_meta_key"><?php esc_html_e( 'Clé meta Photo :', 'certif-ephoto-control' ); ?></label><br>
								<input type="text" id="certif_ephoto_photo_meta_key" name="certif_ephoto_photo_meta_key" value="<?php echo esc_attr( $photo_key ); ?>" class="regular-text" placeholder="<?php esc_attr_e( 'auto-détecté par défaut', 'certif-ephoto-control' ); ?>">
							</p>
							<p>
								<label for="certif_ephoto_sign_meta_key"><?php esc_html_e( 'Clé meta Signature :', 'certif-ephoto-control' ); ?></label><br>
								<input type="text" id="certif_ephoto_sign_meta_key" name="certif_ephoto_sign_meta_key" value="<?php echo esc_attr( $sign_key ); ?>" class="regular-text" placeholder="<?php esc_attr_e( 'auto-détecté par défaut', 'certif-ephoto-control' ); ?>">
							</p>
							<p class="description"><?php esc_html_e( 'Prioritaires sur la détection automatique (article puis commande). La valeur peut être une URL ou un ID de pièce jointe image. Seules les images hébergées dans le dossier uploads de ce site sont acceptées.', 'certif-ephoto-control' ); ?></p>
						</td>
					</tr>
				</table>

				<?php submit_button( __( 'Enregistrer les réglages', 'certif-ephoto-control' ) ); ?>
			</form>

			<hr style="margin:40px 0;">

			<div class="card" style="max-width:100%;padding:20px;">
				<h2><?php esc_html_e( 'Diagnostic : détection sur une commande', 'certif-ephoto-control' ); ?></h2>
				<p class="description"><?php esc_html_e( 'Entrez un ID de commande WooCommerce pour vérifier comment sa photo et sa signature sont détectées.', 'certif-ephoto-control' ); ?></p>

				<form method="post" style="margin-top:15px;display:flex;gap:10px;align-items:center;">
					<?php wp_nonce_field( 'certif_inspect_order_action', 'certif_inspect_nonce' ); ?>
					<label for="certif_inspect_order_id" class="screen-reader-text"><?php esc_html_e( 'ID de commande', 'certif-ephoto-control' ); ?></label>
					<input type="number" id="certif_inspect_order_id" name="certif_inspect_order_id" value="<?php echo esc_attr( $test_order_id ? $test_order_id : '' ); ?>" placeholder="Ex : 1234" class="small-text" style="width:120px;" required>
					<button type="submit" class="button button-secondary"><?php esc_html_e( 'Analyser cette commande', 'certif-ephoto-control' ); ?></button>
				</form>

				<?php if ( null !== $inspect_result ) : ?>
					<div style="margin-top:20px;background:#f8fafc;padding:15px;border-radius:6px;border:1px solid #cbd5e1;">
						<h3><?php echo esc_html( sprintf( __( 'Résultat pour la commande #%d', 'certif-ephoto-control' ), $test_order_id ) ); ?></h3>

						<?php if ( isset( $inspect_result['error'] ) ) : ?>
							<p style="color:#dc2626;"><strong><?php echo esc_html( $inspect_result['error'] ); ?></strong></p>
						<?php elseif ( is_wp_error( $inspect_result['extracted_data'] ) ) : ?>
							<p style="color:#dc2626;"><strong><?php echo esc_html( $inspect_result['extracted_data']->get_error_message() ); ?></strong></p>
						<?php else : ?>
							<?php $ext = $inspect_result['extracted_data']; ?>
							<table class="widefat striped" style="margin-top:10px;max-width:900px;">
								<tr>
									<td style="width:200px;"><strong><?php esc_html_e( 'Client détecté', 'certif-ephoto-control' ); ?></strong></td>
									<td><?php echo esc_html( $ext['customer']['name'] ); ?> (<?php echo esc_html( $ext['customer']['email'] ); ?>)</td>
								</tr>
								<tr>
									<td><strong><?php esc_html_e( 'Photo détectée', 'certif-ephoto-control' ); ?></strong></td>
									<td>
										<?php if ( ! empty( $ext['photo_url'] ) ) : ?>
											<span style="color:#059669;font-weight:600;">✔ <?php esc_html_e( 'Trouvée :', 'certif-ephoto-control' ); ?></span>
											<a href="<?php echo esc_url( $ext['photo_url'] ); ?>" target="_blank" rel="noopener noreferrer"><?php echo esc_html( $ext['photo_url'] ); ?></a>
											<br><img src="<?php echo esc_url( $ext['photo_url'] ); ?>" alt="" referrerpolicy="no-referrer" style="max-height:80px;border-radius:4px;margin-top:6px;border:1px solid #e2e8f0;">
										<?php else : ?>
											<span style="color:#dc2626;font-weight:600;">✘ <?php esc_html_e( 'Aucune photo acceptable trouvée', 'certif-ephoto-control' ); ?></span>
										<?php endif; ?>
									</td>
								</tr>
								<tr>
									<td><strong><?php esc_html_e( 'Signature détectée', 'certif-ephoto-control' ); ?></strong></td>
									<td>
										<?php if ( ! empty( $ext['signature_url'] ) ) : ?>
											<span style="color:#059669;font-weight:600;">✔ <?php esc_html_e( 'Trouvée :', 'certif-ephoto-control' ); ?></span>
											<a href="<?php echo esc_url( $ext['signature_url'] ); ?>" target="_blank" rel="noopener noreferrer"><?php echo esc_html( $ext['signature_url'] ); ?></a>
											<br><img src="<?php echo esc_url( $ext['signature_url'] ); ?>" alt="" referrerpolicy="no-referrer" style="max-height:50px;border-radius:4px;margin-top:6px;background:#fff;border:1px solid #e2e8f0;padding:2px;">
										<?php else : ?>
											<span style="color:#dc2626;font-weight:600;">✘ <?php esc_html_e( 'Aucune signature acceptable trouvée', 'certif-ephoto-control' ); ?></span>
										<?php endif; ?>
									</td>
								</tr>
							</table>
							<p class="description"><?php esc_html_e( 'Seules les images (jpg, png, webp, bmp, tif) hébergées dans le dossier uploads de ce site sont retenues.', 'certif-ephoto-control' ); ?></p>

							<details style="margin-top:15px;">
								<summary style="cursor:pointer;font-weight:600;color:#0284c7;"><?php esc_html_e( 'Afficher les données brutes des articles (TM EPO / meta)', 'certif-ephoto-control' ); ?></summary>
								<pre style="background:#fff;padding:12px;border:1px solid #cbd5e1;border-radius:4px;max-height:350px;overflow:auto;font-size:11px;"><?php echo esc_html( wp_json_encode( $inspect_result['items'], JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES ) ); ?></pre>
							</details>
						<?php endif; ?>
					</div>
				<?php endif; ?>
			</div>
		</div>
		<?php
	}
}
